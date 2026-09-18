import loadPlaid from "./plaidLoader.js";

const PORT = 8080;
const CREATE_LINK_TOKEN_URL = `http://127.0.0.1:${PORT}/create_link_token`;
const EXCHANGE_URL = `http://127.0.0.1:${PORT}/exchange_public_token`;

async function sendPublicToken(public_token, metadata) {
    const res = await fetch(EXCHANGE_URL, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            public_token: public_token,
        }),
    });

    return res;
}

async function start() {
    // load the plaid link script
    const Plaid = await loadPlaid();

    // grab link token from server
    const res = await fetch(CREATE_LINK_TOKEN_URL, {
        method: "POST",
    });

    const { link_token } = await res.json();

    // handle user bank login through plaid
    const handler = Plaid.create({
        token: link_token,
        onSuccess: sendPublicToken,
        onLoad: () => {},
        onExit: (err, metadata) => {
            console.log("Error");
        },
        onEvent: (eventName, metadata) => {},
    });

    const btn = document.getElementById("link-button");

    btn.addEventListener("click", async () => {
        handler.open();
    });
}

start();
