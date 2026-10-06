import os
import sqlite3
from urllib.parse import urlsplit

from flask import Flask, abort, g, jsonify, render_template, request
from jinja2 import select_autoescape
from middleware import authenticate_request, is_development_mode
from models import (
    PlaidItems,
    SimpleFinCredentials,
    UserPlaidConfigs,
    db,
    hash_legacy_simplefin_passwords,
)
from routes import plaid_bp, routing_bp, simplefin_bp, user_bp
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.exceptions import HTTPException

# Machine-to-machine SimpleFIN protocol endpoints called by Actual Budget. They carry
# their own credentials (claim id / HTTP Basic Auth) instead of a Cloudflare session.
MACHINE_ENDPOINTS = {"simplefin.claim_token", "simplefin.get_accounts"}

# Endpoints that verify the Cloudflare session themselves because they accept the
# token of a different Access application than the dashboard's.
SELF_AUTHENTICATED_ENDPOINTS = {"routing.route_actual_user"}


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Ensure SQLite enables WAL mode and foreign keys automatically."""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def create_app():
    app = Flask(__name__)

    # Flask only autoescapes templates ending in .html and friends, which the
    # `.html.jinja` templates used here do not
    app.jinja_env.autoescape = select_autoescape(
        enabled_extensions=("html", "htm", "xml", "xhtml", "svg", "jinja"),
        default_for_string=True,
    )

    app.register_blueprint(user_bp, url_prefix="/api/user")
    app.register_blueprint(plaid_bp, url_prefix="/api/plaid")
    app.register_blueprint(simplefin_bp, url_prefix="/simplefin")
    app.register_blueprint(routing_bp, url_prefix="/auth")

    # Fallback to the mounted /data folder inside the container
    default_db_uri = "sqlite:////data/bridge.db"
    db_uri = os.getenv("DATABASE_URI", default_db_uri)

    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    # Initialize app with extensions
    db.init_app(app)

    with app.app_context():
        # Create sqlite3 database and tables if they do not exist already
        db.create_all()
        hash_legacy_simplefin_passwords()

    app.before_request(check_csrf_origins)
    app.before_request(get_user_credentials)
    app.after_request(prevent_sensitive_response_caching)
    app.register_error_handler(HTTPException, handle_http_error)
    app.add_url_rule("/", view_func=dashboard)
    return app


# Add any external trusted domains (e.g., your separate frontend or payment gateways)
ALLOWED_ORIGINS = {
    # "https://frontend.example.com",
}


def check_csrf_origins():
    # Only protect state-changing requests
    if request.method not in ["POST", "PUT", "DELETE", "PATCH"]:
        return

    # Machine-to-machine endpoints are not authenticated by browser credentials
    if request.endpoint in MACHINE_ENDPOINTS:
        return

    fetch_site = request.headers.get("Sec-Fetch-Site")

    # 1. Modern browser check via Fetch Metadata
    if fetch_site:
        # "same-site" is rejected too: sibling subdomains are not trusted
        if fetch_site not in ["same-origin", "none"]:
            abort(403, description="Cross-origin requests forbidden.")
        return

    # 2. Fallback check for clients lacking Sec-Fetch-Site
    # Check Origin first, then fall back to Referer
    source_url = request.headers.get("Origin") or request.headers.get("Referer")

    # Reject missing origins or sandboxed/opaque origins ("null")
    if not source_url or source_url == "null":
        abort(403, description="Missing or untrusted request origin.")

    parsed = urlsplit(source_url)
    source_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    current_origin = request.host_url.rstrip("/")

    if source_origin != current_origin and source_origin not in ALLOWED_ORIGINS:
        abort(403, description="Cross-origin request rejected.")


def prevent_sensitive_response_caching(response):
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def get_user_credentials():
    if request.endpoint in MACHINE_ENDPOINTS | SELF_AUTHENTICATED_ENDPOINTS:
        return

    _, err_response = authenticate_request()
    if err_response:
        return err_response


def handle_http_error(err: HTTPException):
    """Return structured JSON errors to the dashboard's API calls."""
    if request.path.startswith("/api/"):
        return jsonify({"error": err.name, "message": err.description}), err.code
    return err


def dashboard():
    config = UserPlaidConfigs.query.filter_by(user_id=g.user_id).first()
    items = PlaidItems.query.filter_by(user_id=g.user_id).all()
    credentials = (
        SimpleFinCredentials.query.filter_by(user_id=g.user_id)
        .order_by(SimpleFinCredentials.created_at.desc())
        .all()
    )
    return render_template(
        "dashboard.html.jinja",
        config=config,
        items=items,
        credentials=credentials,
    )


app = create_app()


if __name__ == "__main__":
    # Never expose the Werkzeug debugger (remote code execution) outside local development
    app.run(debug=is_development_mode(), port=8080, host="0.0.0.0")
