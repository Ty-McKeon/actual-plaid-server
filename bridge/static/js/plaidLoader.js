export default function loadPlaid() {
    return new Promise((resolve, reject) => {
        if (window.Plaid) {
            resolve(window.Plaid);
            return;
        }

        const script = document.createElement("script");
        script.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
        script.async = true;

        script.onload = () => resolve(window.Plaid);
        script.onerror = (err) => reject("Failed to load Plaid script!", err);

        document.head.append(script);
    });
}
