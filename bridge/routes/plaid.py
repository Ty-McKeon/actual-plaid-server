from services import PlaidService
from middleware import require_json
from flask import Blueprint, g, jsonify, request
from models import UserPlaidConfigs
from models import db, PlaidItems, UserPlaidConfigs

plaid_bp = Blueprint("plaid", __name__)


def get_plaid_service_for_user(user_id: str = None) -> PlaidService:
    """Retrieves credentials from the DB and returns an initialized PlaidService."""
    if user_id == None:
        user_id = g.user_id

    config = UserPlaidConfigs.query.filter_by(user_id=user_id).first_or_404(
        description="Plaid credentials not configured."
    )
    return PlaidService.from_config(config)


@plaid_bp.post("/api/plaid/create-link-token")
def create_link_token():
    plaid_service = get_plaid_service_for_user()
    link_token = plaid_service.create_link_token()

    return jsonify({"link_token": link_token}), 200


@plaid_bp.post("/api/plaid/exchange-public-token")
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
