import json
from datetime import date

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
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


def plaid_error_message(err: plaid.ApiException) -> str:
    """Extracts the human-readable message from a Plaid API error response."""
    try:
        body = json.loads(err.body)
        return body.get("error_message") or body.get("error_code") or str(err.reason)
    except (TypeError, ValueError, AttributeError):
        return str(err.reason or "Plaid API request failed.")


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

    def create_link_token(self) -> str:
        """Generates an ephemeral link_token required to initialize Plaid Link on the frontend."""

        request = LinkTokenCreateRequest(
            client_name="Actual Budget Bridge",
            language="en",
            country_codes=[CountryCode("US")],
            products=[Products("transactions")],
            user=LinkTokenCreateRequestUser(client_user_id=self.user_id),
        )

        response = self.client.link_token_create(request)
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
        response = self.client.item_public_token_exchange(request)
        return response["access_token"], response["item_id"]

    def remove_item(self, access_token: str) -> None:
        """Removes the Item at Plaid, invalidating its access token and ending billing."""
        self.client.item_remove(ItemRemoveRequest(access_token=access_token))

    def get_accounts(self, access_token: str) -> list[dict]:
        """Fetch all accounts and their balances for a given access token.

        Uses /accounts/get (balances as of Plaid's last update of the Item) rather than
        /accounts/balance/get, which is billed per call and forces a slow real-time
        fetch from the institution on every sync.
        """
        request = AccountsGetRequest(access_token=access_token)
        response = self.client.accounts_get(request)
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
        response = self.client.transactions_get(request)
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
            resp = self.client.transactions_get(request)
            page_txs = [tx.to_dict() for tx in resp["transactions"]]
            if not page_txs:
                break
            raw_txs.extend(page_txs)

        return raw_txs
