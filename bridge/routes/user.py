import middleware.auth as auth
import plaid
from services import PlaidService
from flask import Blueprint, request, jsonify, g
from models import db, UserPlaidConfigs
from middleware import require_json

user_bp = Blueprint("user", __name__)


@user_bp.put("/api/user/plaid-config")
@require_json
def upsert_user_config():
    data = request.get_json()

    client_id = data.get("clientID")
    secret = data.get("secret")

    if not client_id or not secret:
        return jsonify({"error": "Missing client ID or secret"}), 400

    email = g.email
    user_id = g.user_id

    try:
        plaid_service = PlaidService(user_id, client_id, secret, "sandbox")
        plaid_service.create_link_token()
    except plaid.ApiException:
        return (
            jsonify(
                {
                    "error": "Invalid Plaid client ID or secret. Please check your credentials and try again."
                }
            ),
            400,
        )

    # Write credentials to database
    config = UserPlaidConfigs.query.filter_by(user_id=user_id).first()
    if not config:
        config = UserPlaidConfigs()
        config.user_id = user_id
        config.user_email = email
        db.session.add(config)

    config.plaid_client_id = client_id
    config.plaid_secret = secret

    db.session.commit()

    return jsonify({"status": "verified"}), 201
