function setup() {
    const form = document.getElementById("setup-form");

    form.addEventListener("submit", async (e) => {
        e.preventDefault();

        const formData = new FormData(form);
        const formObject = Object.fromEntries(formData);
        const formJSON = JSON.stringify(formObject);

        const res = await fetch("/create-user", {
            method: "PUT",
            headers: {
                "Content-Type": "application/json"
            },
            body: formJSON,
        });

        const data = await res.json();

        console.log({ data });
    });
}

setup();