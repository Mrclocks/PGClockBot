"use strict";
// Loads the Mini App scripts into a VM with a minimal fake browser and Telegram WebApp.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "../app/web/static");
const SCRIPTS = ["catalog_price.js", "miniapp/core.js", "miniapp/render.js", "miniapp/pull.js", "miniapp/app.js"];

function fakeElement(id) {
  const classes = new Set();
  return {
    id, innerHTML: "", textContent: "", hidden: false, disabled: false, isConnected: true,
    dataset: {}, focused: 0,
    classList: {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    focus() { this.focused += 1; },
    addEventListener() {},
    remove() {},
  };
}

/** A button-like element whose attributes drive an ACTIONS handler. */
function trigger(attrs) {
  return {
    disabled: false, isConnected: true, dataset: {}, textContent: attrs.text || "",
    classList: { add() {}, remove() {} },
    getAttribute: (name) => (name in attrs ? String(attrs[name]) : null),
  };
}

/**
 * routes: function(method, url, body) -> {status, body} | Promise thereof, or throws
 * to simulate a lost response.
 */
/**
 * hash: the address fragment at launch (Telegram puts #tgWebAppData=… there).
 * launch: a stand-in for launch.js's PGMiniLaunch ({ initData, sdk: Promise<boolean> }).
 */
function loadMiniApp({ routes, initData = "query_id=x&hash=y", confirm = () => true, telegram = true, pathname = "/miniapp/", hash = "", launch = null }) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, fakeElement(id));
    return elements.get(id);
  };
  const calls = [];
  const back = { visible: false, handler: null };
  const toasts = [];
  const location = { hash, pathname };
  const doc = {
    documentElement: { attrs: {}, setAttribute(name, value) { this.attrs[name] = String(value); } },
    body: { classList: element("body").classList, appendChild(el) { toasts.push(el); } },
    getElementById: (id) => element(id),
    querySelectorAll: () => [],
    createElement: () => ({ setAttribute() {}, remove() {}, className: "", textContent: "" }),
    addEventListener() {},
  };
  const tg = telegram ? {
    initData, colorScheme: "dark", ready() {}, expand() {},
    colors: {}, setHeaderColor(c) { this.colors.header = c; }, setBackgroundColor(c) { this.colors.background = c; },
    events: {}, onEvent(name, fn) { this.events[name] = fn; },
    BackButton: { show() { back.visible = true; }, hide() { back.visible = false; }, onClick(fn) { back.handler = fn; } },
    HapticFeedback: { notificationOccurred() {}, selectionChanged() {} },
    openLink(url) { calls.push({ method: "OPEN", url }); },
    close() { calls.push({ method: "CLOSE" }); },
  } : null;
  const context = {
    console, URL, URLSearchParams, Promise, Map, Set, Intl, Date, JSON, Math, Number, String, Object, Array, Error,
    location,
    history: { replaceState: (_a, _b, hash) => { location.hash = hash; } },
    scrollTo() {},
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout() {},
    confirm,
    open: (url) => calls.push({ method: "OPEN", url }),
    addEventListener() {},
    navigator: {},
    document: doc,
    Telegram: tg ? { WebApp: tg } : undefined,
    PGMini: { autostart: false },
    ...(launch ? { PGMiniLaunch: launch } : {}),
    fetch: async (url, options) => {
      const body = typeof options.body === "string" ? JSON.parse(options.body) : options.body;
      calls.push({ method: options.method, url, body, headers: options.headers });
      const res = await routes(options.method, url, body);
      const headers = res.headers || {};
      return {
        ok: res.status < 400, status: res.status, text: async () => JSON.stringify(res.body), blob: async () => res.blob,
        headers: { get: (name) => (name in headers ? String(headers[name]) : null) },
      };
    },
  };
  context.window = context;
  vm.createContext(context);
  for (const file of SCRIPTS) {
    vm.runInContext(fs.readFileSync(path.join(STATIC, file), "utf8"), context, { filename: file });
  }
  return { context, mini: context.PGMini, element, calls, back, toasts, location };
}

/** Wait until queued promise callbacks (fetch mocks, renders) have run. */
async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
}

module.exports = { loadMiniApp, trigger, settle };
