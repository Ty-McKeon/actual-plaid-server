import os
from urllib.parse import urlsplit

from flask import Flask, abort, g, render_template, request
from models import db
from routes import plaid_bp, simplefin_bp, user_bp


def create_app():
    app = Flask(__name__)

    app.register_blueprint(user_bp, url_prefix="/api/user")
    app.register_blueprint(plaid_bp, url_prefix="/api/plaid")
    app.register_blueprint(simplefin_bp, url_prefix="/simplefin")

    # Fallback to the mounted /data folder inside the container
    default_db_url = "sqlite:////data/bridge.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", default_db_url)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Initialize app with extensions
    db.init_app(app)

    # Ensure SQLite enables WAL mode and foreign keys automatically
    with app.app_context():
        from sqlalchemy import event  # TODO should probably move these
        from sqlalchemy.engine import Engine

        @event.listens_for(Engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        # Create sqlite3 database and tables if they do not exist already
        db.create_all()

    return app


app = create_app()


# Add any external trusted domains (e.g., your separate frontend or payment gateways)
ALLOWED_ORIGINS = {
    # "https://frontend.example.com",
}


@app.before_request
def check_csrf_origins():
    # Only protect state-changing requests
    if request.method not in ["POST", "PUT", "DELETE", "PATCH"]:
        return

    # Skip CSRF check for machine-to-machine protocol endpoints (SimpleFIN)
    if request.path.startswith("/simplefin"):
        return

    fetch_site = request.headers.get("Sec-Fetch-Site")

    # 1. Modern browser check via Fetch Metadata
    if fetch_site:
        if fetch_site not in ["same-origin", "same-site", "none"]:
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


@app.before_request
def get_user_credentials():
    # TODO implement
    g.user_id = "7335d417-61da-459d-899c-0a01c76a2f94"
    g.email = "user@example.com"


@app.route("/")
def dashboard():
    return render_template("dashboard.html.jinja")


if __name__ == "__main__":
    app.run(debug=True, port=8080, host="0.0.0.0")
