"use strict";
// Mini App receipt review: in-app entry, escaping, receipt photo as a blob, one action per tap, way back.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const NAV = {
  admin: [{ id: "home", label: "داشبورد", icon: "home" }, { id: "ops", label: "میانبرها", icon: "ops" }],
  reseller: [{ id: "home", label: "خانه", icon: "home" }, { id: "ops", label: "فروشگاه", icon: "store" }],
};
const me = (persona) => ({
  persona, nav: NAV[persona], user: { id: 1, name: "a" }, currency: "تومان", panel_base: "https://panel.example.com",
  commerce_allowed: persona !== "admin",
  customer: { wallet: 0, wallet_pay_enabled: false, qr_enabled: false, commerce_allowed: persona !== "admin", home: { service_count: 0, attention: [], recent: [] } },
});
const ops = (canReview) => ({
  stats: { pending: 2, tickets: 0 }, can_review: canReview, panel_base: "https://panel.example.com",
  panel_links: [{ id: "finance", label: "مالی", path: "/finance" }], shop: { bot_username: "shop_bot", active: true },
});
const QUEUE = { total: 2, items: [
  { id: 10, amount: 70000, kind: "topup", order_id: null, plan_name: "", payer: HOSTILE, created_at: null },
  { id: 11, amount: 50000, kind: "order", order_id: 20, plan_name: HOSTILE, payer: "b", created_at: null },
] };

function router(persona, overrides, canReview) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) if (pattern.test(method + " " + url)) return handler(body, url);
    if (url === "/api/mini/me") return { status: 200, body: me(persona) };
    if (url === "/api/mini/ops") return { status: 200, body: ops(canReview !== false) };
    if (url === "/api/mini/review") return { status: 200, body: QUEUE };
    if (/\/api\/mini\/review\/\d+\/receipt$/.test(url)) return { status: 200, body: null, blob: new Blob(["jpeg"], { type: "image/jpeg" }) };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function boot(persona, overrides, { confirm = true, canReview } = {}) {
  const env = loadMiniApp({ routes: router(persona, overrides, canReview) });
  env.context.Telegram.WebApp.showConfirm = (_message, done) => done(confirm);
  const revoked = [];
  env.context.URL = Object.assign(function () {}, { createObjectURL: () => "blob:receipt-1", revokeObjectURL: (u) => revoked.push(u) });
  env.revoked = revoked;
  env.mini.app.start();
  await settle();
  return env;
}

async function openQueue(env) {
  env.mini.app.go("review");
  await settle();
}

const posts = (env) => env.calls.filter((c) => c.method === "POST");
const queueReads = (env) => env.calls.filter((c) => c.url === "/api/mini/review").length;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("pending receipts open in the app when the account may review, else the panel", async () => {
  let env = await boot("admin");
  assert.match(env.element("root").innerHTML, /data-action="goto" data-view="review"/);
  env = await boot("admin", [], { canReview: false });
  const html = env.element("root").innerHTML;
  assert.ok(!html.includes('data-view="review"') && html.includes('data-action="open-panel"'));
});

test("the queue escapes payer and plan names and has a way back per persona", async () => {
  let env = await boot("admin");
  await openQueue(env);
  let html = env.element("root").innerHTML;
  assert.ok(html.includes("&lt;img") && !html.includes("<img src=x"));
  assert.ok(html.includes("<bdi>&lt;img"), "payer names keep their own direction (e.g. @username)");
  assert.ok(html.includes("بازگشت به داشبورد"));
  env.mini.app.ACTIONS.back();
  assert.equal(env.mini.app.state.view, "home");
  env = await boot("reseller");
  await openQueue(env);
  html = env.element("root").innerHTML;
  assert.ok(html.includes("بازگشت به فروشگاه"));
  env.mini.app.ACTIONS.back();
  assert.equal(env.mini.app.state.view, "ops");
});

test("the receipt photo is fetched with initData and shown from a blob URL that is revoked on close", async () => {
  const env = await boot("admin");
  await openQueue(env);
  await env.mini.app.ACTIONS["open-review"](trigger({ "data-id": 10 }));
  const receiptCall = env.calls.find((c) => c.url === "/api/mini/review/10/receipt");
  assert.ok(receiptCall && receiptCall.headers["X-Telegram-Init-Data"]);
  const sheet = env.element("sheet-body").innerHTML;
  assert.ok(sheet.includes('src="blob:receipt-1"'));
  assert.ok(sheet.includes("&lt;img") && !sheet.includes("<img src=x"));
  env.mini.app.closeSheet();
  assert.deepEqual(env.revoked, ["blob:receipt-1"]);
});

test("only a blob: URL is ever used as the photo source", async () => {
  const env = await boot("admin");
  env.context.URL.createObjectURL = () => "javascript:alert(1)";
  await openQueue(env);
  await env.mini.app.ACTIONS["open-review"](trigger({ "data-id": 10 }));
  assert.ok(!env.element("sheet-body").innerHTML.includes("javascript:"));
});

test("a failed photo shows a safe message and keeps the actions", async () => {
  const env = await boot("admin", [[/GET \/api\/mini\/review\/10\/receipt/, () => ({ status: 400, body: { detail: "دریافت رسید از تلگرام ناموفق بود" } })]]);
  await openQueue(env);
  await env.mini.app.ACTIONS["open-review"](trigger({ "data-id": 10 }));
  const sheet = env.element("sheet-body").innerHTML;
  assert.ok(sheet.includes("دریافت رسید از تلگرام ناموفق بود") && !sheet.includes("<img class"));
  assert.ok(sheet.includes('data-action="review-approve"'));
});

test("approval needs confirmation, posts once per double tap and reloads the queue", async () => {
  let env = await boot("admin", [], { confirm: false });
  await openQueue(env);
  await env.mini.app.ACTIONS["review-approve"](trigger({ "data-id": 10 }));
  assert.equal(posts(env).length, 0);
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  env = await boot("admin", [[/POST \/api\/mini\/review\/10\/approve/, async () => { await pending; return { status: 200, body: { ok: true, message: "تأیید شد" } }; }]]);
  await openQueue(env);
  const t = trigger({ "data-id": 10 });
  const a = env.mini.app.ACTIONS["review-approve"](t);
  const b = env.mini.app.ACTIONS["review-approve"](t);
  await settle();
  open();
  await Promise.all([a, b]);
  await settle();
  assert.equal(posts(env).length, 1);
  assert.equal(queueReads(env), 2);
  await env.mini.app.ACTIONS["review-approve"](trigger({ "data-id": 99 }));
  assert.equal(posts(env).length, 1, "ids outside the loaded queue are never posted");
});

test("a lost answer is never retried; the queue is read again", async () => {
  const env = await boot("admin", [[/POST \/api\/mini\/review\/11\/reject/, () => { throw new Error("network"); }]]);
  await openQueue(env);
  await env.mini.app.ACTIONS["review-reject"](trigger({ "data-id": 11 }));
  await settle();
  assert.equal(posts(env).length, 1);
  assert.equal(queueReads(env), 2);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); } catch (err) { console.error("FAILED:", name); throw err; }
  }
  console.log(`Mini App receipt review: ${tests.length} checks passed`);
})().catch((err) => { console.error(err); process.exitCode = 1; });
