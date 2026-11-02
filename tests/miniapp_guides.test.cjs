"use strict";
// Mini App connection guides: escaping, safe links, subscription button, send once, back to list.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 0, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};
const GUIDES = { guides: [
  { id: "v2box", title: HOSTILE, body: HOSTILE, opens_subscription: true, media_count: 2,
    links: [{ label: HOSTILE, url: "https://youtu.be/abc" }, { label: "tg", url: "https://t.me/channel" }, { label: "bad", url: "javascript:alert(1)" }] },
  { id: "plain", title: "Plain", body: "text", opens_subscription: false, media_count: 0, links: [] },
] };
const SERVICE = { id: 7, username: "svc", status: "active", status_fa: "فعال", subscription_url: "https://sub.example.com/s/tok", is_cancelled: false };

function router(overrides) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) if (pattern.test(method + " " + url)) return handler(body, url);
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url === "/api/mini/guides") return { status: 200, body: GUIDES };
    if (url === "/api/mini/service/7") return { status: 200, body: { service: SERVICE } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function boot(overrides) {
  const env = loadMiniApp({ routes: router(overrides) });
  env.context.Telegram.WebApp.openTelegramLink = (url) => env.calls.push({ method: "TG_LINK", url });
  env.mini.app.start();
  await settle();
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  await env.mini.app.ACTIONS.guides(trigger({ "data-id": 7 }));
  await settle();
  return env;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("guide list and guide view are escaped and offer a way back to the list", async () => {
  const env = await boot();
  let html = env.element("sheet-body").innerHTML;
  assert.ok(html.includes("&lt;img") && !html.includes("<img src=x"));
  env.mini.app.ACTIONS["open-guide"](trigger({ "data-guide": "v2box", "data-id": 7 }));
  html = env.element("sheet-body").innerHTML;
  assert.ok(html.includes("&lt;img") && !html.includes("<img src=x"));
  assert.ok(html.includes('data-action="guides"'), "back to all guides");
  assert.ok(html.includes("افزودن اشتراک به برنامه"));
  assert.ok(html.includes('data-action="send-guide"'));
  env.mini.app.ACTIONS["open-guide"](trigger({ "data-guide": "plain", "data-id": 7 }));
  html = env.element("sheet-body").innerHTML;
  assert.ok(!html.includes("افزودن اشتراک") && !html.includes('data-action="send-guide"'));
});

test("guide links open only http(s); t.me inside Telegram", async () => {
  const env = await boot();
  env.mini.app.ACTIONS["open-guide"](trigger({ "data-guide": "v2box", "data-id": 7 }));
  for (const i of [0, 1, 2, 9]) env.mini.app.ACTIONS["open-guide-link"](trigger({ "data-guide": "v2box", "data-index": i }));
  assert.deepEqual(env.calls.filter((c) => c.method === "OPEN").map((c) => c.url), ["https://youtu.be/abc"]);
  assert.deepEqual(env.calls.filter((c) => c.method === "TG_LINK").map((c) => c.url), ["https://t.me/channel"]);
});

test("sending the full guide is one request per tap", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot([[/POST \/api\/mini\/guides\/v2box\/send/, async () => { await pending; return { status: 200, body: { ok: true } }; }]]);
  const t = trigger({ "data-guide": "v2box" });
  const a = env.mini.app.ACTIONS["send-guide"](t);
  const b = env.mini.app.ACTIONS["send-guide"](t);
  open();
  await Promise.all([a, b]);
  assert.equal(env.calls.filter((c) => c.method === "POST").length, 1);
  await env.mini.app.ACTIONS["send-guide"](trigger({ "data-guide": "../x" }));
  assert.equal(env.calls.filter((c) => c.method === "POST").length, 1, "unknown guide ids are never sent");
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); } catch (err) { console.error("FAILED:", name); throw err; }
  }
  console.log(`Mini App guides: ${tests.length} checks passed`);
})().catch((err) => { console.error(err); process.exitCode = 1; });
