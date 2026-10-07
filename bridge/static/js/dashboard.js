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

async function sendPublicToken(sessionId, public_token, metadata) {
    const res = await fetch("/api/plaid/exchange-public-token", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            public_token: public_token,
            session_id: sessionId,
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

function refreshConnections() {
    if (window.htmx) {
        window.htmx.ajax("GET", "/api/plaid/items", {
            target: "#connections-container",
            swap: "innerHTML",
        });
    }
}

// A single reservation is shared across tabs because OAuth can return in a new
// tab. Web Locks make creating/changing that reservation atomic across tabs.
// Only the opaque server session ID is needed to resume; the server checks its owner.
const OAUTH_RETURN_PATH = "/oauth-return";
const PENDING_LINK_KEY = "plaidPendingLink";

function withLinkLock(action) {
    if (!navigator.locks) {
        return Promise.reject(new Error("Bank linking is not supported in this browser. Please try another browser."));
    }
    return navigator.locks.request("plaid-link-flow", action);
}

function readPendingLink() {
    const value = localStorage.getItem(PENDING_LINK_KEY);
    if (!value) return null;
    try {
        const pending = JSON.parse(value);
        if (/^[a-f0-9]{64}$/.test(pending?.session_id) && Date.parse(pending.expires_at) > Date.now()) {
            return pending;
        }
    } catch (_) {}
    return null;
}

function showPendingControls() {
    const button = document.getElementById("cancel-pending-link");
    if (button) {
        try { button.hidden = !readPendingLink(); } catch (_) { button.hidden = true; }
    }
}

async function clearPendingLink(sessionId) {
    await withLinkLock(() => {
        if (readPendingLink()?.session_id === sessionId) localStorage.removeItem(PENDING_LINK_KEY);
    });
    showPendingControls();
}

async function saveLinkResult(sessionId, publicToken, metadata) {
    await withLinkLock(() => {
        const pending = readPendingLink();
        if (pending?.session_id === sessionId) {
            localStorage.setItem(PENDING_LINK_KEY, JSON.stringify({ ...pending, publicToken, metadata, finished: true }));
        }
    });
}

async function getLinkSession(sessionId) {
    const res = await fetch(`/api/plaid/link-sessions/${encodeURIComponent(sessionId)}`);
    if (!res.ok) {
        const error = await res.json().catch(() => ({}));
        throw new Error(error.message || "This bank connection cannot be continued by the current user.");
    }
    return res.json();
}

function leaveOAuthReturn() {
    if (window.location.pathname === OAUTH_RETURN_PATH) window.history.replaceState({}, "", "/");
}

async function finishLink(pending, session, publicToken, metadata) {
    if (!session.completed) {
        if (session.item_id !== null) {
            const res = await fetch(`/api/plaid/link-sessions/${pending.session_id}/complete`, { method: "POST" });
            if (!res.ok) throw new Error("Could not confirm the reconnect. Please reload to retry.");
        } else {
            await sendPublicToken(pending.session_id, publicToken, metadata);
        }
    }
    await clearPendingLink(pending.session_id);
    leaveOAuthReturn();
    refreshConnections();
    showToast(session.item_id !== null ? "Institution reconnected." : "Institution connected.", "success");
}

function runPlaidLink(pending, session, { receivedRedirectUri, onDone }) {
    const handler = window.Plaid.create({
        token: session.link_token,
        ...(receivedRedirectUri ? { receivedRedirectUri } : {}),
        onSuccess: async (publicToken, metadata) => {
            try {
                // Retain the callback result if the network fails during exchange.
                await saveLinkResult(pending.session_id, publicToken, metadata);
                await finishLink(pending, session, publicToken, metadata);
            } catch (err) {
                showToast(err.message || "Could not save the connection. Please reload to retry.", "danger");
            } finally {
                handler.destroy();
                onDone();
            }
        },
        onExit: async (err) => {
            try {
                await fetch(`/api/plaid/link-sessions/${pending.session_id}`, { method: "DELETE" });
                await clearPendingLink(pending.session_id);
                leaveOAuthReturn();
            } catch (_) {}
            handler.destroy();
            onDone();
            if (err) showToast(err.display_message || "Bank connection cancelled or failed.", "warning");
        },
    });
    handler.open();
}

async function openPlaidLink(tokenUrl, { onDone }) {
    let pending;
    try {
        await loadPlaid();
        const session = await withLinkLock(async () => {
            if (readPendingLink()) {
                throw new Error("A bank connection is already in progress. Finish it, or cancel the pending connection first.");
            }
            // Verify storage before creating a Link session that needs it to resume.
            localStorage.setItem(PENDING_LINK_KEY, "{}");
            const res = await fetch(tokenUrl, { method: "POST" });
            if (!res.ok) {
                const error = await res.json().catch(() => ({}));
                throw new Error(error.message || error.error || "Could not start bank linking.");
            }
            const session = await res.json();
            pending = { session_id: session.session_id, expires_at: session.expires_at };
            localStorage.setItem(PENDING_LINK_KEY, JSON.stringify(pending));
            return session;
        });
        showPendingControls();
        runPlaidLink(pending, session, { onDone });
    } catch (err) {
        // Keep a created session for recovery or explicit cancellation.
        showToast(err.message || "Could not start bank linking.", "danger");
        onDone();
    }
}

async function resumePlaidLinkAfterRedirect() {
    try {
        if (window.location.pathname !== OAUTH_RETURN_PATH && !readPendingLink()?.finished) {
            showPendingControls();
            return;
        }
        const pending = await withLinkLock(() => readPendingLink());
        if (!pending) {
            if (window.location.pathname === OAUTH_RETURN_PATH) {
                showToast("Could not continue linking your bank. Please start again.", "warning");
            }
            return;
        }
        showPendingControls();
        const returning = window.location.pathname === OAUTH_RETURN_PATH &&
            new URLSearchParams(window.location.search).has("oauth_state_id");
        if (!returning && !pending.publicToken && !pending.finished) return;
        // Check ownership before opening the SDK or exchanging any token.
        const session = await getLinkSession(pending.session_id);
        if (pending.publicToken || pending.finished || session.completed) {
            await finishLink(pending, session, pending.publicToken, pending.metadata);
            return;
        }
        await loadPlaid();
        runPlaidLink(pending, session, { receivedRedirectUri: window.location.href, onDone: () => {} });
    } catch (err) {
        // Preserve both the callback URL and reservation so reload can retry.
        showToast(err.message || "Could not continue bank linking. Please reload to retry.", "danger");
    }
}

resumePlaidLinkAfterRedirect();
window.addEventListener("storage", showPendingControls);

// Use event delegation so dynamically re-rendered elements work
document.addEventListener("click", async (e) => {
    const cancel = e.target.closest("#cancel-pending-link");
    if (cancel) {
        e.preventDefault();
        cancel.disabled = true;
        try {
            await withLinkLock(async () => {
                const pending = readPendingLink();
                if (!pending) return;
                const res = await fetch(`/api/plaid/link-sessions/${pending.session_id}`, { method: "DELETE" });
                if (!res.ok && ![404, 410].includes(res.status)) throw new Error("Could not cancel the pending connection. Please try again.");
                localStorage.removeItem(PENDING_LINK_KEY);
                leaveOAuthReturn();
            });
            showPendingControls();
        } catch (err) { showToast(err.message, "danger"); }
        finally { cancel.disabled = false; }
        return;
    }
    const linkBtn = e.target.closest("#link-btn");
    if (linkBtn && !linkBtn.disabled) {
        e.preventDefault();
        setLinkButtonLoading(true);
        await openPlaidLink("/api/plaid/create-link-token", {
            onDone: () => setLinkButtonLoading(false),
        });
        return;
    }

    // Reconnect signs in to an institution again through Plaid Link's update mode
    const reconnectBtn = e.target.closest("[data-reconnect-item]");
    if (reconnectBtn && !reconnectBtn.disabled) {
        e.preventDefault();
        reconnectBtn.disabled = true;
        const itemId = reconnectBtn.dataset.reconnectItem;
        await openPlaidLink(`/api/plaid/items/${encodeURIComponent(itemId)}/link-token`, {
            itemId,
            institutionName: reconnectBtn.dataset.institutionName,
            onDone: () => {
                reconnectBtn.disabled = false;
            },
        });
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
    // Credential saves also replace the linking panel through an out-of-band swap.
    if (["setup-form-container", "link-account-container"].includes(e.detail.target.id)) {
        showPendingControls();
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
