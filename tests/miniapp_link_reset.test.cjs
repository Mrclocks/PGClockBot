"use strict";
// Mini App link change: offered only when allowed, one request per double tap, the new link escaped.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const NEW_URL = "https://sub.example.com/sub/new-token";

function me() {
  return {
    persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "https://panel.example.com",
    nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" }],
    user: { id: 1, name: "u", role: "user" },
    customer: { wallet: 0, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
      home: { service_count: 1, attention: [], recent: [] } },
  };
}

function service(extra) {
  return { id: 7, username: HOSTILE, status: "active", status_fa: "فعال", traffic: "1 از 10 گیگ", traffic_pct: 10,
    expire_days_label: "۵ روز", subscription_url: "https://sub.example.com/sub/old-token", addons_allowed: false,
    is_cancelled: false, cancellation_pending: false, link_reset_allowed: true, ...(extra || {}) };
}

function routes(state) {
  return async (method, url) => {
    if (url === "/api/mini/me") return { status: 200, body: me() };
    if (url === "/api/mini/service/7") return { status: 200, body: { service: service(state.detail) } };
    if (url === "/api/mini/service/7/link-reset" && method === "GET") return { status: 200, body: state.offer };
    if (url === "/api/mini/service/7/link-reset" && method === "POST") {
      state.posts += 1;
      return { status: 200, body: state.result };
    }
    if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [service()], next_before_id: null } };
    return { status: 404, body: { detail: "not found" } };
  };
}

function form() {
  const button = trigger({});
  return { getAttribute: (name) => (name === "data-form" ? "link-reset" : name === "data-id" ? "7" : null),
    querySelector: () => button, elements: {} };
}

async function boot(state, confirm) {
  const env = loadMiniApp({ routes: routes(state), confirm });
  env.mini.app.start();
  await settle();
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  return env;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the offer sheet shows the warning, the Persian count and one danger button", () => {
  const env = loadMiniApp({ routes: async () => ({ status: 404, body: {} }), telegram: false });
  const { render } = env.mini;
  const open = render.linkResetHtml({ offered: true, left: 2 }, 7);
  assert.ok(open.includes('data-form="link-reset" data-id="7"') && open.includes("۲ بار"));
  assert.ok(open.includes('role="alert"') && open.includes("is-danger"));
  assert.ok(!render.linkResetHtml({ offered: true, left: 0 }, 7).includes("<form"), "no form once the limit is used");
  assert.ok(!render.linkResetHtml({ offered: false }, 7).includes("<form"), "no form when not offered");
});

test("the result shows a safe new link escaped, and a note when the answer is not in yet", () => {
  const env = loadMiniApp({ routes: async () => ({ status: 404, body: {} }), telegram: false });
  const { render } = env.mini;
  const done = render.linkResetResultHtml({ status: "done", subscription_url: NEW_URL + "?a=<b>" }, 7);
  assert.ok(done.includes("new-token") && !done.includes("<b>"), "the link is escaped");
  assert.ok(done.includes('data-action="copy-link" data-id="7"'));
  const unsafe = render.linkResetResultHtml({ status: "done", subscription_url: "javascript:alert(1)" }, 7);
  assert.ok(!unsafe.includes("javascript:"), "a non-http link is never shown");
  assert.ok(render.linkResetResultHtml({ status: "unknown", subscription_url: null }, 7).includes("چند دقیقهٔ دیگر"));
});

test("the detail lists the action only when the server allows it", async () => {
  const allowed = await boot({ offer: {}, result: {}, posts: 0 });
  assert.ok(allowed.element("root").innerHTML.includes('data-action="link-reset" data-id="7"'));
  const denied = await boot({ offer: {}, result: {}, posts: 0, detail: { link_reset_allowed: false } });
  assert.ok(!denied.element("root").innerHTML.includes('data-action="link-reset"'));
});

test("a double tap sends one change and a declined confirm sends none", async () => {
  const state = { offer: { offered: true, left: 2 }, result: { status: "done", subscription_url: NEW_URL }, posts: 0 };
  let answer = false;
  const env = await boot(state, () => answer);
  await env.mini.app.ACTIONS["link-reset"](trigger({ "data-id": 7 }));
  await settle();
  await env.mini.app.submitForm(form());
  await settle();
  assert.equal(state.posts, 0, "nothing is sent without the customer's confirm");
  answer = true;
  const target = form();
  await Promise.all([env.mini.app.submitForm(target), env.mini.app.submitForm(target)]);
  await settle();
  assert.equal(state.posts, 1, "two taps send one change");
  assert.ok(env.element("sheet-body").innerHTML.includes("new-token"), "the sheet shows the new link");
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
  console.log(`Mini App link reset: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
