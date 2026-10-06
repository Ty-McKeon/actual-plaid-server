import loadPlaid from "./plaidLoader.js";

// ============================================================================
// Toast Notification Helper
// ============================================================================

export function showToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.setAttribute("role", "alert");

    const messageSpan = document.createElement("span");
    messageSpan.className = "toast-message";
    messageSpan.textContent = message;

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "toast-close";
    closeBtn.setAttribute("aria-label", "Close notification");
    closeBtn.innerHTML = "&times;";
    closeBtn.addEventListener("click", () => toast.remove());

    toast.appendChild(messageSpan);
    toast.appendChild(closeBtn);
    container.appendChild(toast);

    setTimeout(() => {
        toast.classList.add("toast-fadeout");
        setTimeout(() => toast.remove(), 350);
    }, 4500);
}

window.showToast = showToast;

// ============================================================================
// Copy to Clipboard Helper
// ============================================================================

window.copyToClipboard = function (elementId, button) {
    const input = document.getElementById(elementId);
    if (!input) return;

    const textToCopy = input.value;

    function handleSuccess() {
        const copyTextEl = button?.querySelector(".copy-text");
        const originalText = copyTextEl ? copyTextEl.textContent : "Copy";

        if (copyTextEl) copyTextEl.textContent = "Copied!";
        button?.classList.add("btn-copied");

        showToast("Setup token copied to clipboard!", "success");

        setTimeout(() => {
            if (copyTextEl) copyTextEl.textContent = originalText;
            button?.classList.remove("btn-copied");
        }, 2500);
    }

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard
            .writeText(textToCopy)
            .then(handleSuccess)
            .catch(() => fallbackCopy(input, handleSuccess));
    } else {
        fallbackCopy(input, handleSuccess);
    }
};

function fallbackCopy(input, callback) {
    input.focus();
    input.select();
    try {
        const successful = document.execCommand("copy");
        if (successful && callback) callback();
        if (!successful) showToast("Failed to copy token. Please copy manually.", "danger");
    } catch (err) {
        showToast("Failed to copy token. Please copy manually.", "danger");
    }
}

// ============================================================================
// Plaid Link Workflow
// ============================================================================

// Preload the SDK so the first click on "Link Bank Account" is instant
loadPlaid().catch((err) => {
    console.warn("Could not load Plaid SDK:", err);
});

async function sendPublicToken(public_token, metadata) {
    const res = await fetch("/api/plaid/exchange-public-token", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            public_token: public_token,
            institution_id: metadata?.institution?.institution_id,
            institution_name: metadata?.institution?.name,
        }),
    });

    if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.message || errData.error || "Failed to exchange public token");
    }

    return res;
}

function setLinkButtonLoading(isLoading) {
    const linkBtn = document.getElementById("link-btn");
    const linkSpinner = document.getElementById("link-spinner");
    const linkBtnText = document.getElementById("link-btn-text");

    if (!linkBtn) return;

    if (isLoading) {
        linkBtn.disabled = true;
        if (linkSpinner) linkSpinner.style.display = "inline-block";
        if (linkBtnText) linkBtnText.textContent = "Connecting...";
    } else {
        linkBtn.disabled = false;
        if (linkSpinner) linkSpinner.style.display = "none";
        if (linkBtnText) linkBtnText.textContent = "Link Bank Account";
    }
}

// Use event delegation for #link-btn so dynamically re-rendered elements work
document.addEventListener("click", async (e) => {
    const linkBtn = e.target.closest("#link-btn");
    if (!linkBtn || linkBtn.disabled) return;

    e.preventDefault();

    setLinkButtonLoading(true);
    try {
        await loadPlaid();
    } catch (err) {
        showToast("Failed to load Plaid Link SDK. Please check your network connection.", "danger");
        setLinkButtonLoading(false);
        return;
    }

    try {
        const res = await fetch("/api/plaid/create-link-token", {
            method: "POST",
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(
                err.message || err.error || "Could not initialize Plaid session. Ensure credentials are configured.",
                "danger",
            );
            setLinkButtonLoading(false);
            return;
        }

        const { link_token } = await res.json();

        const handler = window.Plaid.create({
            token: link_token,
            onSuccess: async (public_token, metadata) => {
                try {
                    await sendPublicToken(public_token, metadata);
                    const instName = metadata?.institution?.name || "Financial Institution";
                    showToast(`Successfully connected ${instName}!`, "success");

                    // Trigger HTMX refresh of the connections container
                    if (window.htmx) {
                        window.htmx.ajax("GET", "/api/plaid/items", {
                            target: "#connections-container",
                            swap: "innerHTML",
                        });
                    }
                } catch (exchangeErr) {
                    showToast(exchangeErr.message || "Failed to finalize institution connection.", "danger");
                } finally {
                    setLinkButtonLoading(false);
                }
            },
            onExit: (err, metadata) => {
                setLinkButtonLoading(false);
                if (err) {
                    console.error("Plaid Link Exit Error:", err);
                    showToast(err.display_message || "Plaid Link connection cancelled or failed.", "warning");
                }
            },
            onEvent: (eventName, metadata) => {},
        });

        handler.open();
    } catch (err) {
        console.error("Link error:", err);
        showToast("An unexpected error occurred while starting Plaid Link.", "danger");
        setLinkButtonLoading(false);
    }
});

// ============================================================================
// HTMX Global Event Listeners
// ============================================================================

// HTMX discards 4xx responses by default, which would hide the validation errors the
// server renders into the form partials. Swap those in instead of treating them as failures.
document.addEventListener("htmx:beforeSwap", (e) => {
    const xhr = e.detail.xhr;
    const isHtml = (xhr.getResponseHeader("Content-Type") || "").includes("text/html");
    if ([400, 409].includes(xhr.status) && isHtml) {
        e.detail.shouldSwap = true;
        e.detail.isError = false;
    }
});

document.addEventListener("htmx:afterSwap", (e) => {
    // If the setup form was updated and succeeded, refresh the link account box if needed
    if (e.detail.target.id === "setup-form-container") {
        const linkBtn = document.getElementById("link-btn");
        const isConfigured = e.detail.target.querySelector(".badge-success") !== null;
        if (linkBtn && isConfigured) {
            linkBtn.disabled = false;
        }
    }
});

document.addEventListener("htmx:responseError", (e) => {
    const errorText = e.detail.xhr?.responseText;
    try {
        const parsed = JSON.parse(errorText);
        showToast(parsed.message || parsed.error || "Request failed.", "danger");
    } catch {
        // Fallback for non-JSON errors
        if (e.detail.xhr?.status >= 400 && e.detail.xhr?.status !== 422) {
            showToast("Server request failed. Please check your credentials or logs.", "danger");
        }
    }
});
