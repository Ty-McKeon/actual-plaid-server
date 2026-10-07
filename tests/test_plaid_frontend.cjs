// Run with Node 18+: node --test tests/test_plaid_frontend.cjs
// Executes the shipped dashboard code with isolated browser/SDK fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../bridge/static/js/dashboard.js'), 'utf8')
    .replace('import loadPlaid from "./plaidLoader.js";', '')
    .replace('export function showToast', 'function showToast');
const key = 'plaidPendingLink';
const id = 'a'.repeat(64);
const expires = new Date(Date.now() + 60 * 60 * 1000).toISOString();
const session = { session_id: id, expires_at: expires, link_token: 'link-a', item_id: null, completed: false };
const tick = () => new Promise(resolve => setImmediate(resolve));
function sharedBrowser() {
    let queue = Promise.resolve();
    return {
        storage: new Map(),
        locks: { request(_, action) {
            const result = queue.then(action);
            queue = result.catch(() => {});
            return result;
        } },
    };
}
function fixture({ shared = sharedBrowser(), returning = false, fetch, load } = {}) {
    const calls = [], sdk = [], listeners = {}, errors = [];
    const location = { pathname: returning ? '/oauth-return' : '/', search: returning ? '?oauth_state_id=state-a' : '', href: 'https://bridge.example.com/oauth-return?oauth_state_id=state-a' };
    const context = {
        console: { warn() {}, error() {} }, URLSearchParams, Date, setTimeout() {},
        navigator: { locks: shared.locks },
        localStorage: {
            getItem: k => shared.storage.get(k) || null,
            setItem: (k, v) => shared.storage.set(k, v),
            removeItem: k => shared.storage.delete(k),
        },
        document: { getElementById() { return null; }, addEventListener(name, fn) { listeners[name] = fn; } },
        window: {
            location, addEventListener() {},
            history: { replaceState(_, __, url) { location.pathname = url; location.search = ''; } },
            Plaid: { create(config) { sdk.push(config); return { open() {}, destroy() {} }; } },
        },
        loadPlaid: load || (() => Promise.resolve()),
        fetch: async (url, opts = {}) => {
            calls.push({ url, opts });
            return fetch ? fetch(url, opts) : { ok: true, status: 200, json: async () => session };
        },
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.showToast = message => errors.push(message);
    return { context, shared, calls, sdk, listeners, errors, location };
}
function store(shared, extra = {}) { shared.storage.set(key, JSON.stringify({ session_id: id, expires_at: expires, ...extra })); }
function call(f, code) { return vm.runInContext(code, f.context); }

test('only one tab can start a flow; the other cannot overwrite its reservation', async () => {
    const shared = sharedBrowser();
    const a = fixture({ shared }), b = fixture({ shared });
    await tick();
    await Promise.all([
        call(a, 'openPlaidLink("/api/plaid/create-link-token", {onDone:()=>{}})'),
        call(b, 'openPlaidLink("/api/plaid/create-link-token", {onDone:()=>{}})'),
    ]);
    assert.equal(a.calls.length + b.calls.length, 1);
    assert.equal(a.sdk.length + b.sdk.length, 1);
    assert.equal(JSON.parse(shared.storage.get(key)).session_id, id);
});

test('a callback for an older flow cannot clear the current reservation', async () => {
    const f = fixture(); await tick(); store(f.shared);
    await call(f, `clearPendingLink('${'b'.repeat(64)}')`);
    assert.equal(JSON.parse(f.shared.storage.get(key)).session_id, id);
});

test('SDK failure retains callback URL and state; retry resumes the same token', async () => {
    const shared = sharedBrowser(); store(shared);
    const f = fixture({ shared, returning: true }); await tick();
    f.sdk.length = 0;
    f.context.loadPlaid = () => Promise.reject(new Error('SDK offline'));
    await call(f, 'resumePlaidLinkAfterRedirect()');
    assert.equal(f.sdk.length, 0);
    assert.ok(shared.storage.has(key));
    assert.equal(f.location.search, '?oauth_state_id=state-a');
    f.context.loadPlaid = () => Promise.resolve();
    await call(f, 'resumePlaidLinkAfterRedirect()');
    assert.equal(f.sdk[0].token, 'link-a');
    assert.equal(f.sdk[0].receivedRedirectUri, f.location.href);
});

test('wrong-user callback never opens the SDK or exchanges a public token', async () => {
    const shared = sharedBrowser(); store(shared, { finished: true, publicToken: 'public-a' });
    const f = fixture({ shared, returning: true, fetch: async () => ({ ok: false, status: 404, json: async () => ({ message: 'Wrong user' }) }) });
    await tick();
    assert.equal(f.sdk.length, 0);
    assert.ok(f.calls.every(c => c.opts.method !== 'POST'));
    assert.ok(shared.storage.has(key));
});

test('failed exchange retains callback result and retry sends its original session', async () => {
    const f = fixture(); await tick(); store(f.shared);
    await call(f, 'runPlaidLink(readPendingLink(), {link_token:"link-a",item_id:null,completed:false}, {onDone:()=>{}})');
    f.context.fetch = async () => ({ ok: false, status: 503, json: async () => ({ message: 'Offline' }) });
    await f.sdk[0].onSuccess('public-a', { institution: { name: 'Bank' } });
    assert.equal(JSON.parse(f.shared.storage.get(key)).publicToken, 'public-a');
    f.context.fetch = async (url, opts = {}) => {
        f.calls.push({ url, opts });
        return { ok: true, status: 200, json: async () => session };
    };
    await call(f, 'resumePlaidLinkAfterRedirect()');
    const exchange = f.calls.find(c => c.url === '/api/plaid/exchange-public-token');
    assert.equal(JSON.parse(exchange.opts.body).session_id, id);
    assert.equal(JSON.parse(exchange.opts.body).public_token, 'public-a');
    assert.equal(f.shared.storage.has(key), false);
});

test('reconnect completion uses server item identity and never exchanges a public token', async () => {
    const f = fixture(); await tick(); store(f.shared);
    await call(f, 'runPlaidLink(readPendingLink(), {link_token:"link-a",item_id:12,completed:false}, {onDone:()=>{}})');
    await f.sdk[0].onSuccess(null, {});
    assert.ok(f.calls.some(c => c.url.endsWith('/complete') && c.opts.method === 'POST'));
    assert.ok(!f.calls.some(c => c.url.includes('exchange-public-token')));
    assert.equal(f.shared.storage.has(key), false);
});

test('explicit cancel clears only the owned reservation', async () => {
    const f = fixture(); await tick(); store(f.shared);
    const button = { disabled: false };
    await f.listeners.click({ preventDefault() {}, target: { closest(selector) { return selector === '#cancel-pending-link' ? button : null; } } });
    assert.equal(f.calls[0].opts.method, 'DELETE');
    assert.equal(f.shared.storage.has(key), false);
    assert.equal(button.disabled, false);
});

test('unavailable storage prevents creating a session that cannot survive redirect', async () => {
    const f = fixture(); await tick();
    f.context.localStorage.setItem = () => { throw new Error('Storage blocked'); };
    await call(f, 'openPlaidLink("/api/plaid/create-link-token", {onDone:()=>{}})');
    assert.equal(f.calls.length, 0);
    assert.equal(f.sdk.length, 0);
});
