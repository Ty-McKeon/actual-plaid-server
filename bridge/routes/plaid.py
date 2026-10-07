import logging
import secrets
from datetime import datetime, timedelta, timezone

import plaid
from flask import Blueprint, abort, g, jsonify, render_template, request
from middleware import require_cloudflare_auth, require_json
from middleware.decorators import validate_string_fields
from models import PlaidItems, PlaidLinkSessions, UserPlaidConfigs, db
from services import (
    LOGIN_REQUIRED,
    PlaidService,
    plaid_error_code,
    plaid_error_message,
)

plaid_bp = Blueprint("plaid", __name__)

logger = logging.getLogger(__name__)

# Warn this long before an institution's consent runs out
CONSENT_WARNING = timedelta(days=30)


def _new_link_session(link_token: str, item_id: int | None = None):
    config = UserPlaidConfigs.query.filter_by(user_id=g.user_id).one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    PlaidLinkSessions.query.filter(PlaidLinkSessions.expires_at <= now).delete()
    session = PlaidLinkSessions(
        id=secrets.token_hex(32),
        user_id=g.user_id,
        link_token=link_token,
        client_id=config.plaid_client_id,
        environment=config.plaid_env,
        item_record_id=item_id,
        expires_at=now + timedelta(minutes=30 if item_id else 240),
    )
    db.session.add(session)
    db.session.commit()
    return jsonify(_session_payload(session)), 200


def _session_payload(session):
    return {
        "session_id": session.id,
        "link_token": session.link_token,
        "item_id": session.item_record_id,
        "expires_at": session.expires_at.replace(tzinfo=timezone.utc).isoformat(),
        "completed": session.completed,
    }


def _owned_link_session(session_id):
    session = PlaidLinkSessions.query.filter_by(
        id=session_id, user_id=g.user_id
    ).first_or_404(description="Link session not found for this user.")
    if session.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        abort(410, description="Link session expired. Start linking again.")
    config = UserPlaidConfigs.query.filter_by(user_id=g.user_id).first_or_404()
    if (session.client_id, session.environment) != (
        config.plaid_client_id,
        config.plaid_env,
    ):
        abort(409, description="Plaid configuration changed. Start linking again.")
    return session


@plaid_bp.get("/link-sessions/<session_id>")
@require_cloudflare_auth
def get_link_session(session_id):
    return jsonify(_session_payload(_owned_link_session(session_id)))


@plaid_bp.delete("/link-sessions/<session_id>")
@require_cloudflare_auth
def cancel_link_session(session_id):
    session = _owned_link_session(session_id)
    if session.exchange_started and not session.completed:
        abort(409, description="Connection is being saved. Try again shortly.")
    db.session.delete(session)
    db.session.commit()
    return "", 204


@plaid_bp.post("/link-sessions/<session_id>/complete")
@require_cloudflare_auth
def complete_update_session(session_id):
    session = _owned_link_session(session_id)
    PlaidItems.query.filter_by(
        id=session.item_record_id, user_id=g.user_id
    ).first_or_404(description="Reconnect institution not found.")
    session.completed = True
    db.session.commit()
    return "", 204


@plaid_bp.errorhandler(plaid.ApiException)
def handle_plaid_error(err: plaid.ApiException):
    """Report upstream Plaid failures as structured JSON instead of a bare 500."""
    logger.warning("Plaid API error (HTTP %s)", err.status)
    return (
        jsonify({"error": "Plaid API error", "message": plaid_error_message(err)}),
        502,
    )


def get_plaid_service_for_user(user_id: str | None = None) -> PlaidService:
    """Retrieves credentials from the DB and returns an initialized PlaidService."""
    if user_id is None:
        user_id = g.user_id

    config = UserPlaidConfigs.query.filter_by(user_id=user_id).first_or_404(
        description="Plaid credentials not configured."
    )
    return PlaidService.from_config(config)


@plaid_bp.get("/items")
@require_cloudflare_auth
def list_items():
    """List all connected Plaid institutions for the current user."""
    items = PlaidItems.query.filter_by(user_id=g.user_id).all()
    if request.headers.get("HX-Request"):
        return render_template("partials/connections.html.jinja", items=items)

    return (
        jsonify(
            [
                {
                    "id": item.id,
                    "institution_id": item.institution_id,
                    "institution_name": item.institution_name,
                    "item_id": item.item_id,
                }
                for item in items
            ]
        ),
        200,
    )


@plaid_bp.delete("/items/<int:item_id>")
@require_cloudflare_auth
def delete_item(item_id: int):
    """Disconnect / delete a linked institution."""
    item = PlaidItems.query.filter_by(id=item_id, user_id=g.user_id).first_or_404(
        description="Linked institution not found."
    )
    inst_name = item.institution_name or item.institution_id or "institution"

    # Remove the Item at Plaid as well, otherwise it stays active (and billable) there.
    # Preserve the token on upstream failures so removal can be retried.
    try:
        get_plaid_service_for_user().remove_item(item.access_token)
    except plaid.ApiException as err:
        if plaid_error_code(err) != "ITEM_NOT_FOUND":
            raise

    db.session.delete(item)
    db.session.commit()

    if request.headers.get("HX-Request"):
        items = PlaidItems.query.filter_by(user_id=g.user_id).all()
        return render_template(
            "partials/connections.html.jinja",
            items=items,
            success=f"Successfully disconnected {inst_name}.",
        )

    return jsonify({"status": "deleted", "id": item_id}), 200


def _describe_status(status: dict | None) -> dict:
    """Turns Plaid's view of a connection into what the dashboard shows for it."""
    if status is None:
        return {"state": "unknown", "label": "Status unavailable", "detail": None}

    if status["error_code"] == LOGIN_REQUIRED:
        return {
            "state": "login_required",
            "label": "Reconnect required",
            "detail": "Your bank needs you to sign in again before syncing can resume.",
        }
    if status["error_code"]:
        return {"state": "error", "label": status["error_code"], "detail": None}

    expires = status["consent_expires"]
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        detail = f"Access expires on {expires:%-d %B %Y}."
        if expires - datetime.now(timezone.utc) < CONSENT_WARNING:
            return {"state": "expiring", "label": "Expiring soon", "detail": detail}
        return {"state": "active", "label": "Active", "detail": detail}

    return {"state": "active", "label": "Active", "detail": None}


@plaid_bp.get("/items/<int:item_id>/status")
@require_cloudflare_auth
def item_status(item_id: int):
    """Checks with Plaid whether a linked institution still works.

    The dashboard loads this separately for each institution, so a slow or failing
    Plaid request never holds up the page.
    """
    item = PlaidItems.query.filter_by(id=item_id, user_id=g.user_id).first_or_404(
        description="Linked institution not found."
    )

    try:
        status = get_plaid_service_for_user().get_item_status(item.access_token)
    except plaid.ApiException as err:
        code = plaid_error_code(err)
        logger.warning("Could not get the status of Plaid item %s: %s", item.id, code)
        # Plaid may reject the request itself with the item's own error
        status = (
            {"error_code": code, "consent_expires": None}
            if code == LOGIN_REQUIRED
            else None
        )
    except Exception:
        logger.exception("Could not get the status of Plaid item %s", item.id)
        status = None

    described = _describe_status(status)
    if request.headers.get("HX-Request"):
        return render_template("partials/connection-status.html.jinja", **described)
    return jsonify(described), 200


@plaid_bp.post("/items/<int:item_id>/link-token")
@require_cloudflare_auth
def create_update_link_token(item_id: int):
    """Starts Plaid Link in update mode to sign in to a linked institution again.

    The connection and its account IDs are kept, so accounts already linked in
    Actual Budget carry on syncing afterwards.
    """
    item = PlaidItems.query.filter_by(id=item_id, user_id=g.user_id).first_or_404(
        description="Linked institution not found."
    )
    link_token = get_plaid_service_for_user().create_link_token(item.access_token)

    return _new_link_session(link_token, item.id)


@plaid_bp.post("/create-link-token")
@require_cloudflare_auth
def create_link_token():
    plaid_service = get_plaid_service_for_user()
    link_token = plaid_service.create_link_token()

    return _new_link_session(link_token)


@plaid_bp.post("/exchange-public-token")
@require_cloudflare_auth
@require_json
def exchange_public_token():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Request body must be a JSON object.")
    validate_string_fields(
        data,
        {
            "public_token": 512,
            "session_id": 64,
            "institution_id": 64,
            "institution_name": 255,
        },
    )

    public_token = data.get("public_token")
    if not public_token or not isinstance(public_token, str):
        return (
            jsonify({"error": "Bad Request", "message": "Missing public_token."}),
            400,
        )

    if not data.get("session_id"):
        abort(400, description="Missing Link session.")
    session = _owned_link_session(data["session_id"])
    if session.item_record_id is not None:
        abort(400, description="Reconnect sessions do not exchange public tokens.")
    if session.completed:
        return jsonify({"public_token_exchange": "complete"}), 200
    claimed = PlaidLinkSessions.query.filter_by(
        id=session.id, exchange_started=False, completed=False
    ).update({"exchange_started": True})
    db.session.commit()
    if not claimed:
        abort(409, description="Connection is being saved. Try again shortly.")

    inst_id = data.get("institution_id")
    inst_name = data.get("institution_name")

    plaid_service = get_plaid_service_for_user()
    try:
        access_token, item_id = plaid_service.exchange_public_token(public_token)
    except Exception:
        session.exchange_started = False
        db.session.commit()
        raise

    plaid_item = PlaidItems(
        user_id=g.user_id,
        access_token=access_token,
        item_id=item_id,
        institution_id=inst_id,
        institution_name=inst_name,
    )

    db.session.add(plaid_item)
    session.completed = True
    db.session.commit()

    return jsonify({"public_token_exchange": "complete"}), 201
