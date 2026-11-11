"use strict";
// Shop-owner manage screens: permission shortcuts and hostile-text escaping.
const assert = require("node:assert/strict");
const { loadMiniApp, settle } = require("./miniapp_env.cjs");
// loadMiniApp is also used directly by the client_key retry case.

const HOSTILE = "<img src=x onerror=alert(1)>";

function meReseller() {
  return {
    persona: "reseller",
    commerce_allowed: true,
    currency: "تومان",
    panel_base: "https://panel.example.com",
    nav: [
      { id: "home", label: "خانه", icon: "home" },
      { id: "services", label: "سرویس‌ها", icon: "svc" },
      { id: "shop", label: "خرید", icon: "shop" },
      { id: "ops", label: "فروشگاه", icon: "ops" },
      { id: "account", label: "حساب", icon: "account" },
    ],
    user: { id: 5, name: "Owner", role: "reseller" },
    customer: {
      wallet: 0, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
      home: { service_count: 0, attention: [], recent: [] },
    },
  };
}

function ops(permissions, extra) {
  return {
    stats: { users: 2, orders: 1, pending: 0, services: 0, revenue: 1000, plans: 0, tickets: 1 },
    shop: { bot_username: "shop5bot", active: true, billing: null },
    panel_links: [
      { id: "home", label: "خانه", path: "/", icon: "home" },
      { id: "tickets", label: "تیکت‌ها", path: "/tickets", icon: "tickets" },
    ],
    panel_base: "https://panel.example.com",
    permissions,
    can_review: permissions.includes("payments"),
    ...(extra || {}),
  };
}

function boot(routes) {
  const env = loadMiniApp({
    routes: async (method, url) => {
      if (url === "/api/mini/me") return { status: 200, body: meReseller() };
      if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [], next_before_id: null } };
      for (const [pattern, handler] of routes || []) {
        if (pattern.test(method + " " + url)) return handler();
      }
      return { status: 404, body: { detail: "not found" } };
    },
  });
  env.mini.app.start();
  return settle().then(() => env);
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("ops shortcuts follow permissions from the server", async () => {
  const env = await boot([
    [/GET \/api\/mini\/ops$/, () => ({ status: 200, body: ops(["dashboard", "orders", "tickets"]) })],
  ]);
  env.mini.app.go("ops");
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(html.includes('data-view="shop-customers"'), "customers when dashboard");
  assert.ok(html.includes('data-view="shop-orders"'), "orders when orders");
  assert.ok(html.includes('data-view="shop-tickets"'), "tickets when tickets");
  assert.ok(!html.includes("بررسی رسیدها"), "no review without payments");
});

test("shop customer and ticket lists escape hostile names", async () => {
  const env = await boot([
    [/GET \/api\/mini\/ops$/, () => ({ status: 200, body: ops(["dashboard", "tickets", "payments"]) })],
    [/GET \/api\/mini\/shop\/customers/, () => ({
      status: 200,
      body: { items: [{ id: 10, name: HOSTILE, username: "x", telegram_id: 1, blocked: false }], next_before_id: null },
    })],
    [/GET \/api\/mini\/shop\/tickets$/, () => ({
      status: 200,
      body: { items: [{ id: 30, subject: HOSTILE, status: "open" }], next_before_id: null },
    })],
    [/GET \/api\/mini\/shop\/tickets\/30$/, () => ({
      status: 200,
      body: {
        ticket: { id: 30, subject: HOSTILE, status: "open", user_id: 10 },
        messages: [{ id: 1, is_staff: false, body: HOSTILE }],
      },
    })],
  ]);
  env.mini.app.go("shop-customers");
  await settle();
  let html = env.element("root").innerHTML;
  assert.ok(!html.includes("<img"), "customer name escaped");
  assert.ok(html.includes("&lt;img"));

  env.mini.app.go("shop-tickets");
  await settle();
  html = env.element("root").innerHTML;
  assert.ok(!html.includes("<img"), "ticket subject escaped");

  env.mini.app.go("shop-ticket", { shopTicketId: 30 });
  await settle();
  html = env.element("root").innerHTML;
  assert.ok(!html.includes("<img"), "ticket body escaped");
  assert.ok(html.includes('data-form="shop-ticket-reply"'));
});

test("without dashboard the customer shortcut is hidden", async () => {
  const env = await boot([
    [/GET \/api\/mini\/ops$/, () => ({ status: 200, body: ops(["orders", "tickets", "payments"]) })],
  ]);
  env.mini.app.go("ops");
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(!html.includes('data-view="shop-customers"'));
  assert.ok(html.includes('data-view="shop-orders"'));
});

test("shop ticket reply retries reuse one client_key", async () => {
  let replyCalls = 0;
  const env = loadMiniApp({
    routes: async (method, url, body) => {
      if (url === "/api/mini/me") return { status: 200, body: meReseller() };
      if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [], next_before_id: null } };
      if (url === "/api/mini/ops") return { status: 200, body: ops(["tickets"]) };
      if (url === "/api/mini/shop/tickets/30") {
        return {
          status: 200,
          body: { ticket: { id: 30, subject: "t", status: "open", user_id: 10 }, messages: [] },
        };
      }
      if (method === "POST" && url === "/api/mini/shop/tickets/30/reply") {
        replyCalls += 1;
        if (replyCalls === 1) throw Object.assign(new Error("network"), { status: 0 });
        return { status: 200, body: { ticket_id: 30, message: "ok", duplicate: true } };
      }
      return { status: 404, body: { detail: "not found" } };
    },
  });
  env.mini.app.start();
  await settle();
  env.mini.app.go("shop-ticket", { shopTicketId: 30 });
  await settle();
  const form = {
    attrs: { "data-form": "shop-ticket-reply", "data-id": "30" },
    elements: { body: { value: "سلام" } },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    removeAttribute(name) { delete this.attrs[name]; },
    querySelector() { return { disabled: false }; },
  };
  await env.mini.app.submitForm(form);
  await settle();
  await env.mini.app.submitForm(form);
  await settle();
  const replyBodies = env.calls.filter((c) => c.method === "POST" && String(c.url).includes("/reply")).map((c) => c.body);
  assert.equal(replyBodies.length, 2);
  assert.equal(replyBodies[0].client_key, replyBodies[1].client_key);
  assert.ok(replyBodies[0].client_key.length >= 16);
});

(async () => {
  for (const [name, fn] of tests) {
    await fn();
    console.log("ok - " + name);
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
