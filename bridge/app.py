import os
from routes import user_bp, plaid_bp
from flask import Flask, request, render_template, abort, g
from models import db


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
        from sqlalchemy import event  # TODO should probably move these
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
app.register_blueprint(user_bp)
app.register_blueprint(plaid_bp)


@app.before_request
def check_csrf_origins():
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        origin = request.headers.get("Origin")
        fetch_site = request.headers.get("Sec-Fetch-Site")

        # Block any explicit cross-site fetch
        if fetch_site and fetch_site not in ["same-origin", "same-site", "none"]:
            abort(403, description="Cross-origin requests forbidden.")


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
