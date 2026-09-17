import os
from flask import Flask
from models import db

def create_app():
    app = Flask(__name__)

    # Fallback to the mounted /data folder inside the container
    default_db_url = "sqlite:////data/bridge.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", default_db_url)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Initialize app with extension
    db.init_app(app)


    # Ensure SQLite enables WAL mode and foreign keys automatically
    with app.app_context():
        from sqlalchemy import event
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

@app.route("/")
def dashboard():
    return "<h1>Hello World!</h1>"

if __name__ == "__main__":
    app.run(debug=True, port=8080, host="0.0.0.0")