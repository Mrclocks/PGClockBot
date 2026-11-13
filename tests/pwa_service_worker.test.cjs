"use strict";
// Runs the panel service worker (written by service_worker_js) against a fake cache and network.
const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync(process.argv[2], "utf8");
const ORIGIN = "https://panel.example";

function fakeCaches() {
  const stores = new Map();
  const open = async (name) => {
    if (!stores.has(name)) {
      const entries = new Map();
      stores.set(name, {
        entries,
        match: async (req) => entries.get(req.url),
        // Storing takes a moment, as in a browser: only work kept alive with waitUntil is sure to finish.
        put: (req, res) => new Promise((resolve) => setTimeout(() => { entries.set(req.url, res); resolve(); }, 5)),
        keys: async () => [...entries.keys()].map((url) => ({ url })),
        delete: async (req) => entries.delete(req.url),
        addAll: async () => {},
      });
    }
    return stores.get(name);
  };
  return { open, keys: async () => [...stores.keys()], delete: async (n) => stores.delete(n),
    match: async (req) => { for (const s of stores.values()) { if (s.entries.has(req.url)) return s.entries.get(req.url); } },
    stores };
}

function load() {
  const handlers = {};
  const self = { location: { origin: ORIGIN }, addEventListener: (type, fn) => { handlers[type] = fn; }, skipWaiting: () => {} };
  const caches = fakeCaches();
  const calls = [];
  let nextOk = true;
  const fetch = async (req) => {
    calls.push(req.url);
    const res = { ok: nextOk, status: nextOk ? 200 : 500, body: req.url, clone() { return { ...this }; } };
    return res;
  };
  new Function("self", "caches", "fetch", "URL", source)(self, caches, fetch, URL);
  const request = async (path, method = "GET") => {
    let response;
    const kept = [];
    const event = { request: { url: path.startsWith("http") ? path : ORIGIN + path, method },
      respondWith(p) { response = p; }, waitUntil(p) { kept.push(p); } };
    handlers.fetch(event);
    const res = response ? await response : undefined;
    // The browser may stop the worker once the response and the waitUntil work are done.
    await Promise.all(kept);
    return res;
  };
  return { request, calls, caches, setOk: (v) => { nextOk = v; } };
}

(async () => {
  const keyA = "0123456789abcdef";
  const keyB = "fedcba9876543210";

  // A content-keyed file is fetched once, then served from the cache without the network.
  {
    const sw = load();
    const first = await sw.request(`/static/panel-ds.css?v=${keyA}`);
    const second = await sw.request(`/static/panel-ds.css?v=${keyA}`);
    assert.equal(first.body, `${ORIGIN}/static/panel-ds.css?v=${keyA}`);
    assert.equal(second.body, first.body);
    assert.deepEqual(sw.calls, [`${ORIGIN}/static/panel-ds.css?v=${keyA}`]);
  }

  // The main panel files too, and a new key replaces the old copy instead of piling up.
  {
    const sw = load();
    await sw.request(`/static/panel.css?v=${keyA}`);
    await sw.request(`/static/panel.css?v=${keyB}`);
    await sw.request(`/static/panel.css?v=${keyB}`);
    assert.equal(sw.calls.length, 2);
    const cached = [...sw.caches.stores.values()].flatMap((s) => [...s.entries.keys()]);
    assert.deepEqual(cached, [`${ORIGIN}/static/panel.css?v=${keyB}`]);
  }

  // Without a content key (a version number), panel.css keeps asking the network first.
  {
    const sw = load();
    await sw.request("/static/panel.css?v=0.6.1");
    await sw.request("/static/panel.css?v=0.6.1");
    assert.equal(sw.calls.length, 2);
  }

  // A failed answer is never cached.
  {
    const sw = load();
    sw.setOk(false);
    await sw.request(`/static/panel.js?v=${keyA}`);
    sw.setOk(true);
    await sw.request(`/static/panel.js?v=${keyA}`);
    assert.equal(sw.calls.length, 2);
  }

  // Posts, other origins and non-static paths are left to the browser.
  {
    const sw = load();
    assert.equal(await sw.request(`/static/panel.js?v=${keyA}`, "POST"), undefined);
    assert.equal(await sw.request(`https://cdn.example/static/x.js?v=${keyA}`), undefined);
    assert.equal(await sw.request("/home"), undefined);
    assert.equal(sw.calls.length, 0);
  }

  console.log("pwa service worker tests passed");
})().catch((err) => { console.error(err); process.exit(1); });
