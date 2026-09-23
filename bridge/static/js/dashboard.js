import loadPlaid from "./plaidLoader.js";

await loadPlaid();

const form = document.getElementById("setup-form");
form.addEventListener("submit", async function (e) {
    e.preventDefault();

    const formData = new FormData(form);
    const formObject = Object.fromEntries(formData);
    const formJSON = JSON.stringify(formObject);

    const res = await fetch("/api/user/plaid-config", {
        method: "PUT",
        headers: {
            "Content-Type": "application/json",
        },
        body: formJSON,
    });

    const data = await res.json(); // TODO implement error handling logic

    this.reset();
});

async function sendPublicToken(public_token, metadata) {
    const res = await fetch("/api/plaid/exchange-public-token", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            public_token: public_token,
            institution_id: metadata.institution?.institution_id,
            institution_name: metadata.institution?.name,
        }),
    });

    return res;
}

const linkBtn = document.getElementById("link-btn");
linkBtn.addEventListener("click", async (e) => {
    e.preventDefault();

    // grab link token from server
    const res = await fetch("/api/plaid/create-link-token", {
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

    handler.open();
});
