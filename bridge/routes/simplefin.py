"""Routes for the SimpleFIN Data Format (SDF) bridge protocol.

This module provides endpoints consumed by Actual Budget (or any SimpleFIN client)
to exchange claim tokens and sync financial accounts and transactions.

Protocol Workflow:
------------------
1. Setup Token Generation (`POST /simplefin/token` or dashboard API):
   - Generates a set of credentials (`claim_id`, `username`, `password`) in `SimpleFinCredentials`.
   - Constructs a Claim URL (`<BASE_URL>/simplefin/claim/<claim_id>`).
   - Encodes the Claim URL in Base64 to produce the SimpleFIN "Setup Token".
   - The user inputs this Setup Token into Actual Budget.

2. Claim Exchange (`POST /simplefin/claim/<claim_id>`):
   - Actual Budget decodes the Setup Token to obtain the Claim URL.
   - Actual Budget sends an HTTP POST request to `<BASE_URL>/simplefin/claim/<claim_id>`
     (with empty body / Content-Length: 0).
   - The bridge verifies `claim_id`, ensures `is_claimed` is False, and marks it as claimed.
   - The bridge generates Basic Auth credentials and responds with an "Access URL" as plain text (HTTP 200):
     `https://<username>:<password>@<host>/simplefin`

3. Accounts & Transactions Sync (`GET /simplefin/accounts`):
   - Actual Budget sends periodic GET requests to the Access URL using HTTP Basic Auth.
   - The bridge validates the HTTP Basic Auth credentials against `SimpleFinCredentials`.
   - The bridge resolves the associated `user_id` and queries all linked `PlaidItems`.
   - For each linked institution, `PlaidService` fetches accounts and transactions.
   - Data is transformed via `services/simplefin_service.py` to match the SimpleFIN Data Format (SDF):
       * Inverted transaction amounts: Plaid outflow (+12.50) -> SimpleFIN ("-12.50").
       * Dates converted to UTC Unix epoch timestamps (seconds).
       * Account balances formatted as 2-decimal strings (debts/credit as negative).
       * Structured response envelope: `{"errors": [], "accounts": [...]}`.
"""

import base64
import logging
import secrets
from datetime import datetime, timedelta, timezone

import plaid
from flask import Blueprint, Response, abort, g, jsonify, request
from models import PlaidItems, SimpleFinCredentials, UserPlaidConfigs, db
from services import (
    PlaidService,
    build_access_url,
    build_claim_url,
    build_simplefin_response,
    map_plaid_account_to_simplefin,
)

# Blueprint definition: Registered in app.py with url_prefix="/simplefin"
simplefin_bp = Blueprint("simplefin", __name__)

logger = logging.getLogger(__name__)


def _verify_basic_auth() -> SimpleFinCredentials | None:
    """Extracts and verifies HTTP Basic Auth credentials from the request."""
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return None

    credentials = SimpleFinCredentials.query.filter_by(username=auth.username).first()
    if not credentials or not credentials.is_claimed:
        return None

    if not credentials.password or not secrets.compare_digest(
        credentials.password, auth.password
    ):
        return None

    return credentials


# ============================================================================
# Routes
# ============================================================================


@simplefin_bp.post("/token")
def create_setup_token():
    """Generates a new SimpleFIN Setup Token for the authenticated user.

    The setup token is a Base64-encoded Claim URL that the user copies and
    pastes into Actual Budget's bank sync configuration.
    """
    user_id = g.user_id

    claim_id = secrets.token_hex(16)

    credentials = SimpleFinCredentials(
        user_id=user_id,
        claim_id=claim_id,
        username=None,
        password=None,
    )
    db.session.add(credentials)
    db.session.commit()

    claim_url = build_claim_url(claim_id)
    setup_token = base64.b64encode(claim_url.encode("utf-8")).decode("utf-8")

    return jsonify({"setup_token": setup_token, "claim_url": claim_url}), 201


@simplefin_bp.post("/claim/<claim_id>")
def claim_token(claim_id: str):
    """Exchanges a one-time setup token claim_id for an Access URL.

    Invoked directly by Actual Budget when the user configures bank sync.
    Actual Budget decodes the setup token and performs an HTTP POST request
    to this URL with an empty body (Content-Length: 0).
    """
    credentials = SimpleFinCredentials.query.filter_by(claim_id=claim_id).first_or_404(
        description="Invalid claim id"
    )

    if credentials.is_claimed:
        abort(403, description="Claim token has already been claimed.")

    credentials.is_claimed = True

    username = secrets.token_hex(16)
    password = secrets.token_hex(32)

    credentials.username = username
    credentials.password = password

    db.session.commit()

    access_url = build_access_url(username, password)

    return Response(access_url, status=200, mimetype="text/plain")


@simplefin_bp.get("/accounts")
def get_accounts():
    """Primary SimpleFIN Data Format (SDF) sync endpoint.

    Called periodically by Actual Budget to fetch linked financial accounts
    and recent transactions using HTTP Basic Auth credentials.

    Query Parameters:
    -----------------
    - `start-date` (optional, int): Unix epoch timestamp in seconds.
    - `end-date` (optional, int): Unix epoch timestamp in seconds.
    - `pending` (optional, str/int): If '1', includes pending transactions.
    - `balances-only` (optional, str/int): If '1', returns balances without transactions.
    - `version` (optional, str): SimpleFIN protocol version.
    """
    credential = _verify_basic_auth()
    if not credential:
        return (
            jsonify(
                {"error": "Unauthorized", "message": "Invalid or missing credentials."}
            ),
            401,
            {"WWW-Authenticate": 'Basic realm="SimpleFIN"'},
        )

    # Parse query parameters
    start_date_epoch = request.args.get("start-date", type=int)
    end_date_epoch = request.args.get("end-date", type=int)
    pending = request.args.get("pending") in ("1", "true", "True")
    balances_only = request.args.get("balances-only") in ("1", "true", "True")

    # Determine date range for Plaid transactions
    if end_date_epoch:
        end_date = datetime.fromtimestamp(end_date_epoch, tz=timezone.utc).date()
    else:
        end_date = datetime.now(tz=timezone.utc)

    if start_date_epoch:
        start_date = datetime.fromtimestamp(start_date_epoch, tz=timezone.utc).date()
    else:
        # Default to a 30-day window if start-date is omitted
        start_date = end_date - timedelta(days=30)

    user_id = credential.user_id

    # Retrieve Plaid configuration for user
    config = UserPlaidConfigs.query.filter_by(user_id=user_id).first()
    if not config:
        return (
            jsonify(
                build_simplefin_response(
                    accounts=[],
                    errors=["Plaid credentials not configured for user."],
                )
            ),
            200,
        )

    plaid_items = PlaidItems.query.filter_by(user_id=user_id).all()
    if not plaid_items:
        return jsonify(build_simplefin_response(accounts=[], errors=[])), 200

    plaid_service = PlaidService.from_config(config)

    sdf_accounts = []
    errors = []

    for item in plaid_items:
        try:
            # 1. Fetch accounts and current balances
            plaid_accounts = plaid_service.get_accounts(item.access_token)

            # 2. Fetch transactions if not balances_only
            plaid_transactions = []
            if not balances_only:
                try:
                    plaid_transactions = plaid_service.get_transactions(
                        access_token=item.access_token,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    if not pending:
                        plaid_transactions = [
                            tx
                            for tx in plaid_transactions
                            if not tx.get("pending", False)
                        ]
                except plaid.ApiException as tx_err:
                    errors.append(
                        f"Transactions error for institution {item.institution_name or item.item_id}: "
                        f"{tx_err.body or str(tx_err)}"
                    )

            # Group transactions by account_id
            tx_by_account: dict[str, list[dict]] = {}
            for tx in plaid_transactions:
                acct_id = tx.get("account_id")
                if acct_id:
                    tx_by_account.setdefault(acct_id, []).append(tx)

            # 3. Map accounts and transactions to SimpleFIN format
            for acct in plaid_accounts:
                acct_id = acct.get("account_id")
                acct_txs = tx_by_account.get(acct_id, [])
                sdf_account = map_plaid_account_to_simplefin(acct, acct_txs)
                sdf_accounts.append(sdf_account)

        except plaid.ApiException as item_err:
            errors.append(
                f"Plaid error for institution {item.institution_name or item.item_id}: "
                f"{item_err.body or str(item_err)}"
            )
        except Exception:
            logger.exception(
                "Unexpected error while syncing Plaid item %s", item.item_id
            )
            errors.append(
                f"Unexpected error for {item.institution_name or item.item_id}"
            )

    return jsonify(build_simplefin_response(accounts=sdf_accounts, errors=errors)), 200
