from flask import Blueprint, g, jsonify, render_template, request
from middleware import require_json
from models import PlaidItems, UserPlaidConfigs, db
from services import PlaidService

plaid_bp = Blueprint("plaid", __name__)


def get_plaid_service_for_user(user_id: str | None = None) -> PlaidService:
    """Retrieves credentials from the DB and returns an initialized PlaidService."""
    if user_id is None:
        user_id = g.user_id

    config = UserPlaidConfigs.query.filter_by(user_id=user_id).first_or_404(
        description="Plaid credentials not configured."
    )
    return PlaidService.from_config(config)


@plaid_bp.get("/items")
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
def delete_item(item_id: int):
    """Disconnect / delete a linked institution."""
    item = PlaidItems.query.filter_by(id=item_id, user_id=g.user_id).first_or_404(
        description="Linked institution not found."
    )
    inst_name = item.institution_name or item.institution_id or "institution"
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
def create_link_token():
    plaid_service = get_plaid_service_for_user()
    link_token = plaid_service.create_link_token()

    return jsonify({"link_token": link_token}), 200


@plaid_bp.post("/exchange-public-token")
@require_json
def exchange_public_token():
    data = request.get_json()

    public_token = data.get("public_token")
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
