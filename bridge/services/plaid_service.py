import json
import logging
import os
from datetime import date, datetime
from urllib.parse import urlsplit

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

# Map user string config to official Plaid SDK environment endpoints
ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}

# Maximum page size accepted by /transactions/get
TRANSACTIONS_PAGE_SIZE = 500
REQUEST_TIMEOUT = (5, 20)

# Plaid keeps this much transaction history for a newly linked institution. It is
# fixed when the institution is linked and cannot be raised afterwards.
MAX_HISTORY_DAYS = 730
DEFAULT_HISTORY_DAYS = MAX_HISTORY_DAYS

# Where the dashboard resumes Plaid Link after a bank's own sign-in page sends the
# user back. Registered in app.py; PLAID_REDIRECT_URI must point at it.
OAUTH_RETURN_PATH = "/oauth-return"

logger = logging.getLogger(__name__)

# The institution rejected the stored login, usually because the user's consent
# expired or was revoked. Reconnecting through Plaid Link's update mode fixes it.
LOGIN_REQUIRED = "ITEM_LOGIN_REQUIRED"


def plaid_error_message(err: plaid.ApiException) -> str:
    """Extracts the human-readable message from a Plaid API error response."""
    try:
        body = json.loads(err.body)
        return body.get("error_message") or body.get("error_code") or str(err.reason)
    except (TypeError, ValueError, AttributeError):
        return str(err.reason or "Plaid API request failed.")


def plaid_error_code(err: plaid.ApiException) -> str | None:
    """Extracts Plaid's machine-readable error code from an API error response."""
    try:
        return json.loads(err.body).get("error_code")
    except (TypeError, ValueError, AttributeError):
        return None


def transaction_history_days() -> int:
    """How many days of history to request when an institution is first linked."""
    raw = os.getenv("PLAID_TRANSACTION_HISTORY_DAYS", "").strip()
    if not raw:
        return DEFAULT_HISTORY_DAYS

    try:
        days = int(raw)
    except ValueError:
        days = 0
    if not 1 <= days <= MAX_HISTORY_DAYS:
        raise ValueError(
            f"PLAID_TRANSACTION_HISTORY_DAYS must be between 1 and {MAX_HISTORY_DAYS}."
        )
    return days


def oauth_redirect_uri() -> str | None:
    """The address banks send the user back to after their sign-in page, if set.

    Without one, Plaid Link opens the bank's page in a pop-up, which works on a
    computer but is unreliable on phones. Each user must also add this address to
    the allowed redirect URIs of their own Plaid account.
    """
    uri = os.getenv("PLAID_REDIRECT_URI", "").strip()
    if not uri:
        return None

    parts = urlsplit(uri)
    local = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")
    if (
        not (parts.scheme == "https" or local)
        or not parts.hostname
        or parts.path != OAUTH_RETURN_PATH
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "PLAID_REDIRECT_URI must be an https address ending in "
            f"{OAUTH_RETURN_PATH}, for example https://bridge.example.com"
            f"{OAUTH_RETURN_PATH}."
        )
    return uri


def _rejects_redirect_uri(err: plaid.ApiException) -> bool:
    """Whether Plaid refused a link token because of its redirect address."""
    return (
        plaid_error_code(err) == "INVALID_FIELD"
        and "redirect" in plaid_error_message(err).lower()
    )


class PlaidService:
    def __init__(self, user_id: str, client_id: str, secret: str, env: str = "sandbox"):
        configuration = plaid.Configuration(
            host=ENV_MAP.get(env.lower(), plaid.Environment.Sandbox),
            api_key={"clientId": client_id, "secret": secret},
        )

        api_client = plaid.ApiClient(configuration)
        self.client = plaid_api.PlaidApi(api_client)

        self.user_id = user_id

    @classmethod
    def from_config(cls, config):
        """Factory method to construct the service directly from the DB model."""
        return cls(
            user_id=config.user_id,
            client_id=config.plaid_client_id,
            secret=config.plaid_secret,
            env=config.plaid_env,
        )

    def create_link_token(self, access_token: str | None = None) -> str:
        """Generates an ephemeral link_token required to initialize Plaid Link on the frontend.

        With an `access_token`, Link opens in update mode for that existing
        institution: the user signs in again and the same connection, with the same
        account IDs, carries on working. Without one, Link adds a new institution.
        """
        common = {
            "client_name": "Actual Budget Bridge",
            "language": "en",
            "country_codes": [CountryCode("US")],
            "user": LinkTokenCreateRequestUser(client_user_id=self.user_id),
        }

        if access_token:
            # Update mode takes the existing connection and must not name products
            common["access_token"] = access_token
        else:
            common["products"] = [Products("transactions")]
            common["transactions"] = LinkTokenTransactions(
                days_requested=transaction_history_days()
            )

        redirect_uri = oauth_redirect_uri()
        if redirect_uri:
            try:
                return self._request_link_token(redirect_uri=redirect_uri, **common)
            except plaid.ApiException as err:
                if not _rejects_redirect_uri(err):
                    raise
                # This user has not allowed the address in their Plaid account yet.
                # Linking still works through a pop-up, so carry on without it.
                logger.warning(
                    "Plaid rejected the redirect URI for user %s; using a pop-up.",
                    self.user_id,
                )

        return self._request_link_token(**common)

    def _request_link_token(self, **fields) -> str:
        response = self.client.link_token_create(
            LinkTokenCreateRequest(**fields), _request_timeout=REQUEST_TIMEOUT
        )
        return response["link_token"]

    def exchange_public_token(
        self,
        public_token: str,
    ) -> tuple[str, str]:
        """
        Exchanges the one-time public_token from Plaid Link for a
        permanent access_token and item_id.

        Returns:
            (access_token, item_id)
        """

        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        response = self.client.item_public_token_exchange(
            request, _request_timeout=REQUEST_TIMEOUT
        )
        return response["access_token"], response["item_id"]

    def remove_item(self, access_token: str) -> None:
        """Removes the Item at Plaid, invalidating its access token and ending billing."""
        self.client.item_remove(
            ItemRemoveRequest(access_token=access_token),
            _request_timeout=REQUEST_TIMEOUT,
        )

    def get_item_status(self, access_token: str) -> dict:
        """Reports whether an institution's connection is healthy.

        Returns:
            A dict with `error_code` (None when healthy) and `consent_expires`, the
            time the user's consent runs out if the institution sets one.
        """
        response = self.client.item_get(
            ItemGetRequest(access_token=access_token), _request_timeout=REQUEST_TIMEOUT
        )
        item = response["item"]
        error = item.get("error")
        expires = item.get("consent_expiration_time")
        return {
            "error_code": error.get("error_code") if error else None,
            "consent_expires": expires if isinstance(expires, datetime) else None,
        }

    def get_accounts(self, access_token: str) -> list[dict]:
        """Fetch all accounts and their balances for a given access token.

        Uses /accounts/get (balances as of Plaid's last update of the Item) rather than
        /accounts/balance/get, which is billed per call and forces a slow real-time
        fetch from the institution on every sync.
        """
        request = AccountsGetRequest(access_token=access_token)
        response = self.client.accounts_get(request, _request_timeout=REQUEST_TIMEOUT)
        return [acct.to_dict() for acct in response["accounts"]]

    def get_transactions(
        self,
        access_token: str,
        start_date: date,
        end_date: date,
        account_ids: list[str] | None = None,
    ) -> list[dict]:
        """Fetch transactions between start_date and end_date."""
        options = TransactionsGetRequestOptions(count=TRANSACTIONS_PAGE_SIZE)
        if account_ids:
            options.account_ids = account_ids

        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=options,
        )
        response = self.client.transactions_get(
            request, _request_timeout=REQUEST_TIMEOUT
        )
        raw_txs = [tx.to_dict() for tx in response["transactions"]]
        total_transactions = response.get("total_transactions", len(raw_txs))

        # Paginate if there are more transactions
        while len(raw_txs) < total_transactions:
            options.offset = len(raw_txs)
            request = TransactionsGetRequest(
                access_token=access_token,
                start_date=start_date,
                end_date=end_date,
                options=options,
            )
            resp = self.client.transactions_get(
                request, _request_timeout=REQUEST_TIMEOUT
            )
            page_txs = [tx.to_dict() for tx in resp["transactions"]]
            if not page_txs:
                break
            raw_txs.extend(page_txs)

        return raw_txs
