import requests
import os
from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv, find_dotenv, set_key

DOTENV = find_dotenv(".env.secret") or ".env"
DATABASE = ""

PLAID_BASE_URL = "https://sandbox.plaid.com"
CREATE_LINK_TOKEN_ENDPOINT = f"{PLAID_BASE_URL}/link/token/create"
EXCHANGE_PUBLIC_TOKEN_ENDPOINT = f"{PLAID_BASE_URL}/item/public_token/exchange"

app = Flask(__name__)
session = requests.session()


@app.route("/setup")
def setup() -> str:
    # get port from .env
    port = os.getenv("PORT")

    return render_template("setup.html.jinja", port=port)


@app.post("/create_link_token")
def create_link_token():
    body = {
        "client_name": "simplefin-emulator",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": "me"},
        "products": ["transactions"],
    }

    res = session.post(CREATE_LINK_TOKEN_ENDPOINT, json=body)
    return jsonify(res.json())


@app.post("/exchange_public_token")
def exchange_public_token():
    # Get public token from incoming request and swap it for access and item id
    public_token = request.json["public_token"]
    res = session.post(
        EXCHANGE_PUBLIC_TOKEN_ENDPOINT, json={"public_token": public_token}
    )
    res = res.json()

    access_token = res["access_token"]
    item_id = res["item_id"]

    # Put access token and item id into dotenv
    set_key(DOTENV, "ACCESS_TOKEN", access_token)
    set_key(DOTENV, "ITEM_ID", item_id)

    return jsonify({"public_token_exchange": "complete"})


@app.route("/")
def hello_world():
    return f"<h1>Hello World</h1>"


def main():
    # Load secrets from .env file
    load_dotenv(DOTENV)

    # Add Client ID and Secret Key to all requests made with session
    # session.headers.update(
    #     {
    #         "PLAID-CLIENT-ID": os.environ["CLIENT_ID"],
    #         "PLAID-SECRET": os.environ["SECRET"],
    #     }
    # )

    # Run server on port specified in .env file, or default 8080
    app.run(debug=True, port=int(os.getenv("PORT") or "8080"))


if __name__ == "__main__":
    main()
