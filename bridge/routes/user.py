import plaid
from flask import Blueprint, abort, g, jsonify, render_template, request
from middleware import require_cloudflare_auth, require_json
from middleware.decorators import validate_string_fields
from models import PlaidItems, UserPlaidConfigs, db
from services import PlaidService

user_bp = Blueprint("user", __name__)


@user_bp.get("/plaid-config")
@require_cloudflare_auth
def get_user_config():
    """Retrieve user Plaid config or render the setup-form partial."""
    config = UserPlaidConfigs.query.filter_by(user_id=g.user_id).first()
    edit_mode = request.args.get("edit") in ("1", "true", "True")

    if request.headers.get("HX-Request"):
        return render_template(
            "partials/setup-form.html.jinja",
            config=config,
            edit=edit_mode,
        )

    if not config:
        return jsonify({"configured": False}), 200

    return jsonify(config.to_dict()), 200


@user_bp.put("/plaid-config")
@require_cloudflare_auth
@require_json
def upsert_user_config():
    """Save and verify Plaid API credentials for the user."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    validate_string_fields(
        data,
        {
            "clientID": 128,
            "client_id": 128,
            "secret": 512,
            "env": 32,
            "plaid_env": 32,
        },
    )
    client_id = (data.get("clientID") or data.get("client_id") or "").strip()
    secret = (data.get("secret") or "").strip()
    env = (data.get("env") or data.get("plaid_env") or "sandbox").strip().lower()

    if env not in ("sandbox", "production"):
        abort(400, description="env must be sandbox or production.")

    if not client_id or not secret:
        err = "Missing Plaid Client ID or Secret."
        if request.headers.get("HX-Request"):
            existing = UserPlaidConfigs.query.filter_by(user_id=g.user_id).first()
            return (
                render_template(
                    "partials/setup-form.html.jinja",
                    config=existing,
                    error=err,
                    edit=True,
                ),
                400,
            )
        return jsonify({"error": err}), 400

    email = g.email
    user_id = g.user_id

    # Access tokens are bound to the client ID and environment that created them, so
    # switching either would leave every linked institution unusable (and impossible
    # to remove at Plaid). Rotating only the secret is fine.
    existing = UserPlaidConfigs.query.filter_by(user_id=user_id).first()
    if existing and (
        existing.plaid_client_id != client_id or existing.plaid_env != env
    ):
        linked = PlaidItems.query.filter_by(user_id=user_id).count()
        if linked:
            err = (
                f"{linked} linked institution(s) belong to the current Plaid client ID "
                "and environment. Disconnect them before switching to a different one."
            )
            if request.headers.get("HX-Request"):
                return (
                    render_template(
                        "partials/setup-form.html.jinja",
                        config=existing,
                        error=err,
                        edit=True,
                    ),
                    409,
                )
            return jsonify({"error": err}), 409

    try:
        plaid_service = PlaidService(user_id, client_id, secret, env)
        plaid_service.create_link_token()
    except plaid.ApiException:
        err = "Invalid Plaid client ID or secret. Please verify your credentials and environment."
        if request.headers.get("HX-Request"):
            existing = UserPlaidConfigs.query.filter_by(user_id=user_id).first()
            temp_config = existing or UserPlaidConfigs(
                plaid_client_id=client_id, plaid_env=env
            )
            return (
                render_template(
                    "partials/setup-form.html.jinja",
                    config=temp_config,
                    error=err,
                    edit=True,
                ),
                400,
            )
        return (
            jsonify({"error": err}),
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
    config.plaid_env = env

    db.session.commit()

    if request.headers.get("HX-Request"):
        return (
            render_template(
                "partials/credentials-saved.html.jinja",
                config=config,
                success="Credentials verified and saved successfully!",
                edit=False,
            ),
            200,
        )

    return jsonify({"status": "verified"}), 201
