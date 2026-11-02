"use strict";
// Mini App support: escaping, single submit, lost responses, closed tickets, Back order, contact links.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 0, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};
const SUPPORT = {
  text: HOSTILE,
  contacts: [{ title: HOSTILE, url: "https://t.me/support_team" }, { title: "bad", url: "javascript:alert(1)" }],
  tickets: [{ id: 5, subject: HOSTILE, status: "answered", status_fa: "پاسخ‌داده‌شده" }],
};
const thread = (extra) => ({ ticket: { id: 5, subject: HOSTILE, status: "open", status_fa: "باز", is_closed: false,
  messages: [{ id: 1, from_staff: true, body: HOSTILE, created_at: null }], ...(extra || {}) } });

function router(overrides) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) {
      if (pattern.test(method + " " + url)) return handler(body, url);
    }
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url === "/api/mini/support") return { status: 200, body: SUPPORT };
    if (url === "/api/mini/support/tickets/5") return { status: 200, body: thread() };
    if (url.startsWith("/api/mini/activity") || url === "/api/mini/topup") return { status: 200, body: { items: [], recent: [] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function boot(overrides) {
  const env = loadMiniApp({ routes: router(overrides) });
  env.context.Telegram.WebApp.openTelegramLink = (url) => env.calls.push({ method: "TG_LINK", url });
  env.mini.app.start();
  await settle();
  return env;
}

function form(kind, fields, id) {
  const button = { disabled: false, isConnected: true };
  const elements = {};
  for (const [k, v] of Object.entries(fields)) elements[k] = { value: v };
  return {
    elements,
    getAttribute: (name) => ({ "data-form": kind, "data-id": id == null ? null : String(id) }[name] ?? null),
    querySelector: () => button,
  };
}

const posts = (env, part) => env.calls.filter((c) => c.method === "POST" && c.url.includes(part));
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("support page and thread escape every customer and admin text", async () => {
  const env = await boot();
  env.mini.app.go("support");
  await settle();
  let html = env.element("root").innerHTML;
  assert.ok(html.includes("&lt;img") && !html.includes("<img src=x"));
  env.mini.app.go("ticket", { ticketId: 5 });
  await settle();
  html = env.element("root").innerHTML;
  assert.ok(html.includes("&lt;img") && !html.includes("<img src=x"));
  assert.ok(html.includes('data-form="ticket-reply"'));
});

test("contact links open only t.me addresses", async () => {
  const env = await boot();
  env.mini.app.go("support");
  await settle();
  env.mini.app.ACTIONS["open-contact"](trigger({ "data-index": 1 }));
  env.mini.app.ACTIONS["open-contact"](trigger({ "data-index": 7 }));
  assert.equal(env.calls.filter((c) => c.method === "TG_LINK" || c.method === "OPEN").length, 0);
  env.mini.app.ACTIONS["open-contact"](trigger({ "data-index": 0 }));
  assert.deepEqual(env.calls.filter((c) => c.method === "TG_LINK").map((c) => c.url), ["https://t.me/support_team"]);
});

test("a new ticket is sent once and opens its thread", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot([[/POST \/api\/mini\/support\/tickets$/, async () => { await pending; return { status: 200, body: { ticket_id: 5, message: "ok" } }; }]]);
  env.mini.app.go("support");
  await settle();
  await env.mini.app.submitForm(form("ticket-new", { subject: " ", body: "x" }));
  assert.equal(posts(env, "/support/tickets").length, 0, "an empty subject is never sent");
  const f = form("ticket-new", { subject: "vpn", body: "down" });
  const first = env.mini.app.submitForm(f);
  const second = env.mini.app.submitForm(f);
  open();
  await Promise.all([first, second]);
  await settle();
  assert.equal(posts(env, "/support/tickets").length, 1);
  assert.deepEqual(posts(env, "/support/tickets")[0].body, { subject: "vpn", body: "down" });
  assert.equal(env.mini.app.state.view, "ticket");
  assert.equal(env.mini.app.state.ticketId, 5);
});

test("a reply is sent once; a lost reply is not resent and the thread is re-read", async () => {
  let lose = false;
  const env = await boot([[/POST \/api\/mini\/support\/tickets\/5\/messages/, () => {
    if (lose) throw new Error("lost");
    return { status: 200, body: { ticket_id: 5 } };
  }]]);
  env.mini.app.go("ticket", { ticketId: 5 });
  await settle();
  const f = form("ticket-reply", { body: "any news?" }, 5);
  await Promise.all([env.mini.app.submitForm(f), env.mini.app.submitForm(f)]);
  await settle();
  assert.equal(posts(env, "/messages").length, 1);
  lose = true;
  const reads = env.calls.filter((c) => c.url === "/api/mini/support/tickets/5").length;
  await env.mini.app.submitForm(form("ticket-reply", { body: "again" }, 5));
  await settle();
  assert.equal(posts(env, "/messages").length, 2);
  assert.ok(env.calls.filter((c) => c.url === "/api/mini/support/tickets/5").length > reads);
});

test("a closed ticket offers no reply form", async () => {
  const env = await boot([[/GET \/api\/mini\/support\/tickets\/5$/, () => ({ status: 200, body: thread({ status: "closed", is_closed: true }) })]]);
  env.mini.app.go("ticket", { ticketId: 5 });
  await settle();
  assert.ok(!env.element("root").innerHTML.includes('data-form="ticket-reply"'));
});

test("Back goes ticket → support → account → home", async () => {
  const env = await boot();
  env.mini.app.go("ticket", { ticketId: 5 });
  await settle();
  assert.ok(env.element("nav").innerHTML.includes('data-view="account" aria-current="page"'));
  env.back.handler();
  assert.equal(env.mini.app.state.view, "support");
  env.back.handler();
  assert.equal(env.mini.app.state.view, "account");
  env.back.handler();
  assert.equal(env.mini.app.state.view, "home");
});

test("sub-pages show a visible back button to their parent", async () => {
  const env = await boot();
  env.mini.app.go("ticket", { ticketId: 5 });
  await settle();
  assert.ok(env.element("root").innerHTML.includes('data-action="back"'));
  assert.ok(env.element("root").innerHTML.includes("بازگشت به پشتیبانی"));
  env.mini.app.ACTIONS.back(trigger({}));
  assert.equal(env.mini.app.state.view, "support");
  env.mini.app.go("account");
  await settle();
  assert.ok(!env.element("root").innerHTML.includes('data-action="back"'), "tabs need no back button");
});

test("admin persona cannot reach support screens", async () => {
  const env = await boot([[/GET \/api\/mini\/me/, () => ({ status: 200, body: { ...ME, persona: "admin", commerce_allowed: false } })]]);
  env.mini.app.go("support");
  assert.equal(env.mini.app.state.view, "home");
  env.mini.app.go("ticket", { ticketId: 5 });
  assert.equal(env.mini.app.state.view, "home");
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
  console.log(`Mini App support: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
