let plaidPromise = null;

export default function loadPlaid() {
    if (window.Plaid) return Promise.resolve(window.Plaid);

    // Share one in-flight load between callers instead of injecting the script twice
    if (!plaidPromise) {
        plaidPromise = new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
            script.async = true;

            script.onload = () => resolve(window.Plaid);
            script.onerror = () => {
                // Allow a later call to retry
                plaidPromise = null;
                script.remove();
                reject(new Error("Failed to load Plaid script!"));
            };

            document.head.append(script);
        });
    }

    return plaidPromise;
}
