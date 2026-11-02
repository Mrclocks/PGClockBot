"use strict";
// Mini App club: escaping, invite sharing, one redemption per tap, no automatic retry, history paging.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 0, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};

function clubBody(overrides) {
  return { club: {
    enabled: true, points: 300, tier: { name: HOSTILE, points_to_next: 200 }, month_points: 40,
    referral: { code: "REF1", link: "https://t.me/ClockBot?start=ref_REF1", invited: 2, earned_points: 20 },
    rewards: [
      { id: 1, name: HOSTILE, description: HOSTILE, type: "wallet_credit", value: 20000, cost: 100, needs_service: false, affordable: true },
      { id: 3, name: "traffic", description: "", type: "traffic_gb", value: 5, cost: 200, needs_service: true, affordable: true },
      { id: 6, name: "big", description: "", type: "wallet_credit", value: 1, cost: 900, needs_service: false, affordable: false },
    ],
    services: [{ id: 10, name: HOSTILE }],
    discounts: [{ code: HOSTILE, percent: 15, min_purchase: 0, max_discount: null, expires_at: null }],
    history: [{ id: 30, amount: 30, label: HOSTILE, created_at: null }],
    history_more: true,
    ...overrides,
  } };
}

function router(overrides, club) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) if (pattern.test(method + " " + url)) return handler(body, url);
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url === "/api/mini/club") return { status: 200, body: club || clubBody() };
    if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
    if (url === "/api/mini/topup") return { status: 200, body: { recent: [] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function boot(overrides, { club, confirm } = {}) {
  const env = loadMiniApp({ routes: router(overrides, club) });
  env.context.Telegram.WebApp.openTelegramLink = (url) => env.calls.push({ method: "TG_LINK", url });
  env.context.Telegram.WebApp.showConfirm = (_message, done) => done(confirm === undefined ? true : confirm);
  env.mini.app.start();
  await settle();
  env.mini.app.go("club");
  await settle();
  return env;
}

const posts = (env) => env.calls.filter((c) => c.method === "POST");
const clubReads = (env) => env.calls.filter((c) => c.url === "/api/mini/club").length;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("club screen escapes every customer and admin text and disables unaffordable rewards", async () => {
  const env = await boot();
  const html = env.element("root").innerHTML;
  assert.ok(html.includes("&lt;img") && !html.includes("<img src=x"));
  assert.ok(html.includes('data-action="back"'), "the club has a way back to the account");
  assert.match(html, /data-action="redeem-reward" data-id="6" disabled/);
  assert.ok(html.includes("https://t.me/ClockBot?start=ref_REF1"));
  assert.ok(html.includes('data-action="club-history-more"'));
});

test("invites are shared through Telegram and an unsafe link is never shown or opened", async () => {
  let env = await boot();
  env.mini.app.ACTIONS["share-invite"]();
  assert.deepEqual(env.calls.filter((c) => c.method === "TG_LINK").map((c) => c.url),
    ["https://t.me/share/url?url=" + encodeURIComponent("https://t.me/ClockBot?start=ref_REF1")]);
  env = await boot([], { club: clubBody({ referral: { code: "REF1", link: "javascript:alert(1)", invited: 0, earned_points: 0 } }) });
  assert.ok(!env.element("root").innerHTML.includes("javascript:"));
  env.mini.app.ACTIONS["share-invite"]();
  assert.equal(env.calls.filter((c) => c.method === "TG_LINK" || c.method === "OPEN").length, 0);
});

test("a declined confirmation spends nothing; a double tap posts once and reloads the club and wallet", async () => {
  let env = await boot([], { confirm: false });
  await env.mini.app.ACTIONS["redeem-reward"](trigger({ "data-id": 1 }));
  assert.equal(posts(env).length, 0);
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  env = await boot([[/POST \/api\/mini\/club\/rewards\/1\/redeem/, async () => { await pending; return { status: 200, body: { points: 200, reward_type: "wallet_credit" } }; }]]);
  const t = trigger({ "data-id": 1 });
  const a = env.mini.app.ACTIONS["redeem-reward"](t);
  const b = env.mini.app.ACTIONS["redeem-reward"](t);
  open();
  await Promise.all([a, b]);
  await settle();
  assert.equal(posts(env).length, 1);
  assert.equal(clubReads(env), 2, "the club is read again after spending points");
  assert.ok(env.calls.filter((c) => c.url === "/api/mini/me").length >= 2, "the wallet is read again after a wallet reward");
  await env.mini.app.ACTIONS["redeem-reward"](trigger({ "data-id": 6 }));
  await env.mini.app.ACTIONS["redeem-reward"](trigger({ "data-id": 99 }));
  assert.equal(posts(env).length, 1, "unaffordable and unknown rewards are never posted");
});

test("a service reward asks for the service and sends only its id", async () => {
  const env = await boot([[/POST \/api\/mini\/club\/rewards\/3\/redeem/, () => ({ status: 200, body: { points: 100 } })]]);
  await env.mini.app.ACTIONS["redeem-reward"](trigger({ "data-id": 3 }));
  const sheet = env.element("sheet-body").innerHTML;
  assert.ok(sheet.includes("&lt;img") && !sheet.includes("<img src=x"));
  assert.equal(posts(env).length, 0, "nothing is spent before a service is picked");
  await env.mini.app.ACTIONS["redeem-reward-service"](trigger({ "data-id": 3, "data-service": 10 }));
  assert.deepEqual(posts(env).map((c) => c.body), [{ service_id: 10 }]);
});

test("a lost answer is never retried; the club is read again instead", async () => {
  const env = await boot([[/POST \/api\/mini\/club\/rewards\/1\/redeem/, () => { throw new Error("network"); }]]);
  await env.mini.app.ACTIONS["redeem-reward"](trigger({ "data-id": 1 }));
  await settle();
  assert.equal(posts(env).length, 1);
  assert.equal(clubReads(env), 2);
});

test("a discount reward shows its code escaped", async () => {
  const env = await boot([[/POST \/api\/mini\/club\/rewards\/1\/redeem/, () => ({ status: 200, body: { points: 200, discount_code: HOSTILE } })]]);
  await env.mini.app.ACTIONS["redeem-reward"](trigger({ "data-id": 1 }));
  const sheet = env.element("sheet-body").innerHTML;
  assert.ok(sheet.includes("&lt;img") && !sheet.includes("<img src=x"));
  assert.ok(sheet.includes('data-action="copy-value"'));
});

test("more history continues after the last loaded entry", async () => {
  const env = await boot([[/GET \/api\/mini\/club\/history/, () => ({ status: 200, body: { items: [{ id: 12, amount: -5, label: "older", created_at: null }], has_more: false } })]]);
  await env.mini.app.ACTIONS["club-history-more"](trigger({}));
  assert.ok(env.calls.some((c) => c.url === "/api/mini/club/history?before=30"));
  const html = env.element("root").innerHTML;
  assert.ok(html.includes("older") && !html.includes('data-action="club-history-more"'));
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); } catch (err) { console.error("FAILED:", name); throw err; }
  }
  console.log(`Mini App club: ${tests.length} checks passed`);
})().catch((err) => { console.error(err); process.exitCode = 1; });
