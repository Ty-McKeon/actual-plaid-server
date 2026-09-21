import os
import auth
import requests
import constants
from flask import Flask, request, jsonify, render_template, abort
from models import db, UserPlaidConfigs

def create_app():
    app = Flask(__name__)

    # Fallback to the mounted /data folder inside the container
    default_db_url = "sqlite:////data/bridge.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", default_db_url)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Initialize app with extensions
    db.init_app(app)


    # Ensure SQLite enables WAL mode and foreign keys automatically
    with app.app_context():
        from sqlalchemy import event # TODO should probably move these
        from sqlalchemy.engine import Engine

        @event.listens_for(Engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        # Create sqlite3 databse and tables if they do not exist already
        db.create_all()

    return app

app = create_app()


@app.before_request
def check_csrf_origins():
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        origin = request.headers.get("Origin")
        fetch_site = request.headers.get("Sec-Fetch-Site")

        # Block any explicit cross-site fetch
        if fetch_site and fetch_site not in ["same-origin", "same-site", "none"]:
            abort(403, description="Cross-origin requests forbidden.")


@app.route("/")
def dashboard():
    return render_template('dashboard.html.jinja')


@app.put("/create-user")
def create_user():
    if not request.is_json:
        return jsonify({"error": "Payload must be JSON"}), 400

    data = request.get_json()

    client_id = data.get("clientID")
    secret = data.get("secret")

    if not client_id or not secret:
        return jsonify({"error": "Missing client ID or secret"}), 400

    email = auth.get_user_email()
    sub = auth.get_user_sub()


    headers = {
        "PLAID-CLIENT-ID": client_id,
        "PLAID-SECRET": secret,
    }

    body = {
        "client_name": "simplefin-emulator",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": sub},
        "products": ["transactions"],
    }

    try: 
        # Raise error if plaid rejects credentials
        res = requests.post(constants.PLAID_SANDBOX + constants.CREATE_LINK_TOKEN_ENDPOINT, headers=headers, json=body)
        res.raise_for_status()
    except requests.HTTPError as e:
        return jsonify({"error": "Plaid rejected these credentials. Check your Client ID, Secret, and Environment."}), 400

    # Write credentials to database
    config = UserPlaidConfigs.query.filter_by(user_id=sub).first()
    if not config:
        config = UserPlaidConfigs()
        config.user_id = sub
        config.user_email = email
        db.session.add(config)

    config.plaid_client_id = client_id
    config.plaid_secret = secret

    db.session.commit()

    return jsonify({"status": "verified"}), 200


if __name__ == "__main__":
    app.run(debug=True, port=8080, host="0.0.0.0")