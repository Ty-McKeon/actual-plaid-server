import json
import logging

import plaid
from flask import Blueprint, abort, g, jsonify, render_template, request
from middleware import require_cloudflare_auth, require_json
from middleware.decorators import validate_string_fields
from models import PlaidItems, UserPlaidConfigs, db
from services import PlaidService, plaid_error_message

plaid_bp = Blueprint("plaid", __name__)

logger = logging.getLogger(__name__)


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
        try:
            error_code = json.loads(err.body).get("error_code")
        except (TypeError, ValueError, AttributeError):
            error_code = None
        if error_code != "ITEM_NOT_FOUND":
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


@plaid_bp.post("/create-link-token")
@require_cloudflare_auth
def create_link_token():
    plaid_service = get_plaid_service_for_user()
    link_token = plaid_service.create_link_token()

    return jsonify({"link_token": link_token}), 200


@plaid_bp.post("/exchange-public-token")
@require_cloudflare_auth
@require_json
def exchange_public_token():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Request body must be a JSON object.")
    validate_string_fields(data, {
        "public_token": 512, "institution_id": 64, "institution_name": 255,
    })

    public_token = data.get("public_token")
    if not public_token or not isinstance(public_token, str):
        return (
            jsonify({"error": "Bad Request", "message": "Missing public_token."}),
            400,
        )

    inst_id = data.get("institution_id")
    inst_name = data.get("institution_name")

    plaid_service = get_plaid_service_for_user()
    access_token, item_id = plaid_service.exchange_public_token(public_token)

    plaid_item = PlaidItems(
        user_id=g.user_id,
        access_token=access_token,
        item_id=item_id,
        institution_id=inst_id,
        institution_name=inst_name,
    )

    db.session.add(plaid_item)
    db.session.commit()

    return jsonify({"public_token_exchange": "complete"}), 201
