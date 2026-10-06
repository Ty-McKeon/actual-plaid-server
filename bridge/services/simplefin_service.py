"""SimpleFIN service utilities and data mappers.

Provides URL construction helpers and transformations to convert Plaid SDK
responses into the SimpleFIN Data Format (SDF).
"""

import os
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def get_base_url() -> str:
    """Retrieves and normalizes the BASE_URL for SimpleFIN URLs.

    Ensures that a valid HTTP/HTTPS scheme is present and trailing slashes
    or redundant /simplefin suffixes are properly handled.
    """
    base_url = os.getenv("BASE_URL", "").strip()
    if not base_url:
        base_url = "http://flask_bridge:8080"
    elif not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"

    base_url = base_url.rstrip("/")
    base_url = base_url.removesuffix("/simplefin")

    return base_url


def build_claim_url(claim_id: str) -> str:
    """Constructs the absolute Claim URL for a given claim_id.

    Args:
        claim_id: Unique 64-character lookup token.

    Returns:
        Full claim URL string.
    """
    base_url = get_base_url()
    return f"{base_url}/simplefin/claim/{claim_id}"


def build_access_url(username: str, password: str) -> str:
    """Constructs the permanent Access URL with embedded HTTP Basic Auth credentials.

    Args:
        username: SimpleFin credential username.
        password: SimpleFin credential password.

    Returns:
        Full Access URL string containing embedded basic auth credentials.
    """
    base_url = get_base_url()
    parts = urlsplit(base_url)
    netloc = f"{username}:{password}@{parts.netloc}"
    base_path = parts.path.rstrip("/")
    path = f"{base_path}/simplefin"
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


def format_amount(amount: float | str) -> str:
    """Inverts Plaid transaction amount for SimpleFIN Data Format (SDF).

    Plaid represents expenses/outflows as positive numbers (e.g. 12.50).
    SimpleFIN and Actual Budget require expenses as negative numbers ("-12.50").
    Format: String with exactly two decimal places.
    """
    amt = float(amount)
    return f"{-amt:.2f}"


def format_balance(balance: float | str, account_type: str | None = None) -> str:
    """Formats account balance with 2 decimal places.

    For credit cards and loans, ensure balances represent debt as negative numbers.
    """
    bal = float(balance)
    if account_type and account_type.lower() in ("credit", "loan"):
        bal = -abs(bal)
    return f"{bal:.2f}"


def to_epoch(val: Any) -> int:
    """Converts a date, datetime, or ISO string to an integer UTC Unix epoch timestamp."""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        return int(val.replace(tzinfo=timezone.utc).timestamp())
    if isinstance(val, date):
        return int(
            datetime(val.year, val.month, val.day, tzinfo=timezone.utc).timestamp()
        )
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val[:10])
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            pass
    return int(datetime.now(timezone.utc).timestamp())


def map_plaid_transaction_to_simplefin(tx: dict) -> dict:
    """Transforms a Plaid transaction dictionary into SimpleFIN format."""
    posted_date = tx.get("date")
    authorized_date = tx.get("authorized_date") or posted_date

    mapped: dict[str, Any] = {
        "id": str(tx.get("transaction_id", "")),
        "posted": to_epoch(posted_date),
        "amount": format_amount(tx.get("amount", 0.0)),
        "description": tx.get("name") or tx.get("original_description") or "",
        "transacted_at": to_epoch(authorized_date),
        "pending": bool(tx.get("pending", False)),
    }
    if tx.get("merchant_name"):
        mapped["payee"] = tx.get("merchant_name")
    return mapped


def map_plaid_account_to_simplefin(
    account: dict,
    transactions: list[dict] | None = None,
    institution_name: str | None = None,
    institution_id: str | None = None,
) -> dict:
    """Transforms a Plaid account dictionary and its transactions into SimpleFIN format."""
    balances = account.get("balances", {})
    current_balance = balances.get("current", 0.0)
    available_balance = balances.get("available")
    acct_type = str(account.get("type", "")).lower()

    inst_name = institution_name or account.get("name") or "Financial Institution"
    inst_id = institution_id or str(account.get("account_id", ""))

    mapped: dict[str, Any] = {
        "id": str(account.get("account_id", "")),
        "name": account.get("name") or account.get("official_name") or "Account",
        "currency": balances.get("iso_currency_code")
        or balances.get("unofficial_currency_code")
        or "USD",
        "balance": format_balance(current_balance, acct_type),
        "balance-date": int(datetime.now(timezone.utc).timestamp()),
        "transactions": [
            map_plaid_transaction_to_simplefin(t) for t in (transactions or [])
        ],
        "org": {
            "name": inst_name,
            "domain": inst_id,
            "id": inst_id,
            "sfin-url": "",
            "url": "",
        },
    }
    if available_balance is not None:
        mapped["available-balance"] = format_balance(available_balance, acct_type)

    return mapped


def build_simplefin_response(
    accounts: list[dict],
    errors: list[str] | None = None,
) -> dict:
    """Wraps accounts and errors into the SimpleFIN response envelope."""
    return {
        "errors": errors or [],
        "accounts": accounts,
    }
