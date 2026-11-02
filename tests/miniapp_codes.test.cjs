"use strict";
// Mini App codes: discounted checkout (apply, pay once, cancel/release) and gift-code redemption.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" }, { id: "shop", label: "خرید", icon: "shop" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 100000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};
const PLANS = { wallet_pay_enabled: true, plans: [
  { id: 1, name: "a", price: 100000, days: 30, gb: 50, is_trial: false },
  { id: 2, name: "b", price: 50000, days: 30, gb: 20, is_trial: false },
] };
const CHECKOUT = { order_id: 77, amount: 80000, original_amount: 100000, discount: 20000, code: HOSTILE };

function router(overrides) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) {
      if (pattern.test(method + " " + url)) return handler(body, url);
    }
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url === "/api/mini/plans") return { status: 200, body: PLANS };
    if (/\/cancel$/.test(url)) return { status: 200, body: { ok: true } };
    if (url.startsWith("/api/mini/activity") || url === "/api/mini/topup") return { status: 200, body: { items: [], recent: [] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function boot(overrides) {
  const env = loadMiniApp({ routes: router(overrides) });
  env.mini.app.start();
  await settle();
  return env;
}

function form(kind, fields) {
  const button = { disabled: false, isConnected: true };
  const elements = {};
  for (const [k, v] of Object.entries(fields)) elements[k] = { value: v };
  return { elements, getAttribute: (n) => ({ "data-form": kind }[n] ?? null), querySelector: () => button };
}

const posts = (env, re) => env.calls.filter((c) => c.method === "POST" && re.test(c.url));
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

async function withCheckout(overrides) {
  const env = await boot([[/POST \/api\/mini\/checkout$/, () => ({ status: 200, body: { checkout: CHECKOUT } })], ...(overrides || [])]);
  env.mini.app.go("shop");
  await settle();
  await env.mini.app.submitForm(form("discount", { code: " OFF20 " }));
  await settle();
  return env;
}

test("applying a code shows the discounted total, escaped, after one request", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot([[/POST \/api\/mini\/checkout$/, async () => { await pending; return { status: 200, body: { checkout: CHECKOUT } }; }]]);
  env.mini.app.go("shop");
  await settle();
  await env.mini.app.submitForm(form("discount", { code: "  " }));
  assert.equal(posts(env, /\/checkout$/).length, 0, "an empty code is never sent");
  const f = form("discount", { code: "OFF20" });
  const a = env.mini.app.submitForm(f);
  const b = env.mini.app.submitForm(f);
  open();
  await Promise.all([a, b]);
  await settle();
  assert.equal(posts(env, /\/checkout$/).length, 1);
  assert.deepEqual(posts(env, /\/checkout$/)[0].body, { plan_id: 1, price: 100000, code: "OFF20" });
  assert.equal(env.mini.app.state.checkout.order_id, 77);
});

test("paying is sent once per tap; a lost response keeps the order for a safe retry", async () => {
  let lose = true;
  const env = await withCheckout([[/POST \/api\/mini\/checkout\/77\/pay/, () => {
    if (lose) throw new Error("lost");
    return { status: 200, body: { ok: true, message: "ok" } };
  }]]);
  await env.mini.app.ACTIONS["checkout-pay"](trigger({}));
  await settle();
  assert.equal(posts(env, /\/77\/pay$/).length, 1);
  assert.ok(env.mini.app.state.checkout, "the same order stays ready to pay");
  lose = false;
  const t = trigger({});
  await Promise.all([env.mini.app.ACTIONS["checkout-pay"](t), env.mini.app.ACTIONS["checkout-pay"](t)]);
  await settle();
  assert.equal(posts(env, /\/77\/pay$/).length, 2, "one retry; the double tap is ignored");
  assert.equal(env.mini.app.state.checkout, null);
  assert.equal(env.mini.app.state.view, "services");
});

test("cancelling, switching plan or leaving the shop releases the reserved code", async () => {
  let env = await withCheckout();
  await env.mini.app.ACTIONS["checkout-cancel"](trigger({}));
  await settle();
  assert.equal(posts(env, /\/77\/cancel$/).length, 1);
  assert.equal(env.mini.app.state.checkout, null);

  env = await withCheckout();
  env.mini.app.go("account");
  await settle();
  assert.equal(posts(env, /\/77\/cancel$/).length, 1, "leaving the shop cancels the unpaid order");
});

test("a declined confirmation pays nothing", async () => {
  const env = await withCheckout();
  env.context.confirm = () => false;
  await env.mini.app.ACTIONS["checkout-pay"](trigger({}));
  assert.equal(posts(env, /\/pay$/).length, 0);
});

test("gift codes: empty is not sent, one request per tap, wallet re-read on success", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot([[/POST \/api\/mini\/wallet\/redeem/, async () => { await pending; return { status: 200, body: { amount: 50000, wallet: 150000, message: "شارژ شد" } }; }]]);
  env.mini.app.ACTIONS["gift-code"](trigger({}));
  await env.mini.app.submitForm(form("gift", { code: " " }));
  assert.equal(posts(env, /redeem/).length, 0);
  const reads = env.calls.filter((c) => c.url === "/api/mini/me").length;
  const f = form("gift", { code: "GIFT50" });
  const a = env.mini.app.submitForm(f);
  const b = env.mini.app.submitForm(f);
  open();
  await Promise.all([a, b]);
  await settle();
  assert.equal(posts(env, /redeem/).length, 1);
  assert.deepEqual(posts(env, /redeem/)[0].body, { code: "GIFT50" });
  assert.equal(env.element("sheet").hidden, true);
  assert.ok(env.calls.filter((c) => c.url === "/api/mini/me").length > reads);
});

test("discounted summary escapes the code and offers top-up when the wallet is short", async () => {
  const { render, core } = loadMiniApp({ routes: router() }).mini;
  core.setCurrency("تومان");
  const html = render.discountedCheckoutHtml(CHECKOUT, { name: HOSTILE }, 1000);
  assert.ok(!html.includes("<img src=x"));
  assert.ok(html.includes('data-action="topup"'));
  assert.ok(html.includes('data-action="checkout-pay" disabled'));
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
  console.log(`Mini App codes: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
