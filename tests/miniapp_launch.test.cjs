"use strict";
// Mini App launch without the Telegram SDK: sign-in from the address, waiting for a slow SDK,
// a late SDK, the blocked-SDK notice, and Telegram's parameters never read as a route.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { loadMiniApp, settle } = require("./miniapp_env.cjs");

const INIT = "query_id=AA&user=%7B%22id%22%3A7%7D&auth_date=1&hash=abc";
const TG_HASH = "#tgWebAppData=" + encodeURIComponent(INIT) + "&tgWebAppVersion=7.10&tgWebAppPlatform=tdesktop";

function me() {
  return {
    persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
    nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "support", label: "پشتیبانی", icon: "svc" }],
    user: { id: 1, name: "x", role: "user" },
    customer: { wallet: 0, wallet_pay_enabled: false, qr_enabled: false, commerce_allowed: true,
      home: { service_count: 0, attention: [], recent: [] } },
  };
}

const routes = async (_method, url) => {
  if (url === "/api/mini/me") return { status: 200, body: me() };
  if (url === "/api/mini/support") return { status: 200, body: { tickets: [], enabled: true } };
  return { status: 404, body: { detail: "پیدا نشد" } };
};

function loadLaunch(hash) {
  const context = { URLSearchParams, Promise, location: { hash }, document: { getElementById: () => null } };
  context.window = context;
  vm.createContext(context);
  const file = path.join(__dirname, "../app/web/static/miniapp/launch.js");
  vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: "launch.js" });
  return context.PGMiniLaunch;
}

function fakeTelegram(calls) {
  return {
    initData: "", colorScheme: "dark",
    ready() { calls.push({ method: "READY" }); }, expand() {},
    setHeaderColor() {}, setBackgroundColor() {}, onEvent() {},
    BackButton: { show() {}, hide() {}, onClick() { calls.push({ method: "BACK_BOUND" }); } },
  };
}

async function boot(options) {
  const env = loadMiniApp({ routes, telegram: false, ...options });
  env.mini.app.start();
  await settle();
  return env;
}

function meCall(env) {
  return env.calls.find((c) => c.url === "/api/mini/me");
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("launch.js keeps Telegram's initData from the address, with or without a route before it", () => {
  assert.equal(loadLaunch(TG_HASH).initData, INIT);
  assert.equal(loadLaunch("#support?" + TG_HASH.slice(1)).initData, INIT);
  assert.equal(loadLaunch("#home").initData, "");
  assert.equal(loadLaunch("#tgWebAppData=" + "a".repeat(9000)).initData, "");
});

test("without the SDK the app signs in with the initData from the address", async () => {
  const env = await boot({ hash: TG_HASH });
  assert.equal(meCall(env).headers["X-Telegram-Init-Data"], INIT);
  assert.ok(env.element("nav").innerHTML.length > 0);
});

test("a blocked SDK with no sign-in explains the VPN case instead of «open from the button»", async () => {
  const env = await boot({ launch: { initData: "", sdk: Promise.resolve(false) } });
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(html.includes("VPN"));
  assert.equal(meCall(env), undefined);
});

test("while the SDK is still loading the address is left for it to read", async () => {
  const env = await boot({ hash: TG_HASH, launch: { initData: INIT, sdk: new Promise(() => {}) } });
  assert.equal(meCall(env).headers["X-Telegram-Init-Data"], INIT);
  assert.equal(env.location.hash, TG_HASH);
});

test("an SDK that arrives after the first screen still gets connected", async () => {
  let arrive;
  const sdk = new Promise((resolve) => { arrive = resolve; });
  const env = await boot({ hash: TG_HASH, launch: { initData: INIT, sdk } });
  assert.ok(!env.calls.some((c) => c.method === "READY"));
  env.context.Telegram = { WebApp: fakeTelegram(env.calls) };
  arrive(true);
  await settle();
  assert.ok(env.calls.some((c) => c.method === "READY"));
  assert.ok(env.calls.some((c) => c.method === "BACK_BOUND"));
});

test("Telegram's parameters are not a route; a route before them still opens", async () => {
  const plain = await boot({ hash: TG_HASH });
  assert.equal(plain.mini.app.state.view, "home");
  const deep = await boot({ hash: "#support?" + TG_HASH.slice(1) });
  assert.equal(deep.mini.app.state.view, "support");
});

(async () => {
  for (const [name, fn] of tests) {
    try {
      await fn();
    } catch (err) {
      console.error("FAILED:", name);
      throw err;
    }
  }
  console.log(`Mini App launch: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
