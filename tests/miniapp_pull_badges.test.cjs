"use strict";
// Mini App pull-to-refresh and shortcut badges.
const assert = require("node:assert/strict");
const { loadMiniApp, settle } = require("./miniapp_env.cjs");

const NAV = {
  user: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" },
    { id: "shop", label: "خرید", icon: "shop" }, { id: "account", label: "حساب", icon: "account" }],
  admin: [{ id: "home", label: "داشبورد", icon: "home" }, { id: "ops", label: "میانبرها", icon: "ops" }],
};

function me(persona, extra) {
  const commerce = persona !== "admin";
  return {
    persona, nav: NAV[persona], commerce_allowed: commerce, currency: "تومان", panel_base: "https://panel.example.com",
    user: { id: 1, name: "a" }, brand: { name: "Nova" },
    customer: { wallet: 0, wallet_pay_enabled: commerce, qr_enabled: true, commerce_allowed: commerce,
      home: { service_count: 0, attention: [], recent: [] }, badges: { support: 3, club: 120 } },
    ...(extra || {}),
  };
}

const OPS = (canReview) => ({ stats: { pending: 4, tickets: 2 }, can_review: canReview, panel_base: "https://panel.example.com",
  panel_links: [{ id: "finance", label: "مالی", path: "/finance" }, { id: "tickets", label: "تیکت‌ها", path: "/tickets" }] });

async function boot(body, { canReview = true, pathname } = {}) {
  const env = loadMiniApp({
    pathname,
    routes: async (_m, url) => {
      if (url === "/api/mini/me") return { status: 200, body };
      if (url === "/api/mini/ops") return { status: 200, body: OPS(canReview) };
      if (url.startsWith("/api/mini/support/tickets/7/messages")) return { status: 200, body: { ok: true } };
      if (url === "/api/mini/support/tickets/7") return { status: 200, body: { ticket: { id: 7, subject: "s", status: "open", status_fa: "باز", messages: [] } } };
      return { status: 404, body: { detail: "پیدا نشد" } };
    },
  });
  env.swipesOff = 0;
  env.context.Telegram.WebApp.disableVerticalSwipes = () => { env.swipesOff += 1; };
  env.mini.app.start();
  await settle();
  return env;
}

/** A fake page for pull.js: listeners by event name and a style-able indicator. */
function fakePage({ scrollTop = 0, canStart = true } = {}) {
  const listeners = {};
  const classes = new Set();
  const indicator = {
    style: {},
    classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  };
  const page = { listeners, classes, indicator, refreshes: 0, top: scrollTop, allow: canStart };
  page.doc = { addEventListener: (name, fn, opts) => { listeners[name] = fn; page.passive = (page.passive || true) && !!(opts && opts.passive); } };
  return page;
}

async function pull(page, fromY, toY, touches = 1) {
  page.listeners.touchstart({ touches: Array.from({ length: touches }, () => ({ clientY: fromY })) });
  page.listeners.touchmove({ touches: [{ clientY: toY }] });
  await page.listeners.touchend({});
}

/** The markup of the one shortcut tile whose attributes include ``marker``. */
function tile(html, marker) {
  const part = html.split('<button type="button" class="shortcut"').slice(1).find((p) => p.includes(marker));
  return part ? part.slice(0, part.indexOf("</button>")) : "";
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("a long pull at the top refreshes once; short, scrolled, multi-touch or blocked pulls do not", async () => {
  const env = await boot(me("user"));
  const { attach, ARM_PX, DAMPING } = env.mini.pull;
  const page = fakePage();
  let release;
  attach({
    doc: page.doc, indicator: page.indicator, scrollTop: () => page.top, canStart: () => page.allow,
    onRefresh: () => { page.refreshes += 1; return new Promise((r) => { release = r; }); },
  });
  assert.ok(page.passive, "listeners are passive so scrolling is never blocked");
  const far = 100 + ARM_PX / DAMPING + 10;
  await pull(page, 100, 100 + (ARM_PX / DAMPING) * 0.6);
  assert.equal(page.refreshes, 0, "a short pull only peeks");
  const first = pull(page, 100, far);
  assert.ok(page.classes.has("is-loading"));
  await pull(page, 100, far);
  assert.equal(page.refreshes, 1, "a second pull while refreshing is ignored");
  release();
  await first;
  assert.ok(!page.classes.has("is-loading"), "the spinner hides when the refresh ends");
  page.top = 40;
  await pull(page, 100, far);
  page.top = 0;
  await pull(page, 100, far, 2);
  page.allow = false;
  await pull(page, 100, far);
  assert.equal(page.refreshes, 1);
});

test("pull to refresh re-reads /me and the current screen; Telegram's swipe-to-close is off", async () => {
  const env = await boot(me("user"));
  assert.equal(env.swipesOff, 1);
  const before = env.calls.filter((c) => c.url === "/api/mini/me").length;
  await env.mini.app.refreshScreen();
  await settle();
  assert.equal(env.calls.filter((c) => c.url === "/api/mini/me").length, before + 1);
});

test("customer badges: counts on support and club, «۹۹+» past 99, none for zero or closed sections", async () => {
  let env = await boot(me("user"));
  let html = env.element("root").innerHTML;
  assert.ok(tile(html, 'data-view="support"').includes('<span class="shortcut-badge" aria-hidden="true">۳</span>'));
  assert.ok(tile(html, 'data-view="club"').includes('<span class="shortcut-badge" aria-hidden="true">۹۹+</span>'));
  assert.ok(html.includes("، ۳ مورد"), "screen readers hear the count");
  const quiet = me("user");
  quiet.customer.badges = { support: 0, club: -2 };
  env = await boot(quiet);
  assert.ok(!env.element("root").innerHTML.includes("shortcut-badge"));
  env = await boot(me("user", { sections: ["services", "shop"], shop: { name: "s" } }), { pathname: "/miniapp/s/Ab3_-xY9Ab3_-xY9Ab3_-x/" });
  assert.ok(!env.element("root").innerHTML.includes("shortcut-badge"), "closed sections show neither tile nor badge");
});

test("staff badges: receipts on in-app review, open tickets on the panel tile, receipts on finance only without review", async () => {
  let env = await boot(me("admin"));
  let html = env.element("root").innerHTML;
  assert.ok(tile(html, 'data-view="review"').includes('shortcut-badge" aria-hidden="true">۴<'));
  assert.ok(tile(html, 'data-path="/tickets"').includes('shortcut-badge" aria-hidden="true">۲<'));
  assert.ok(!tile(html, 'data-path="/finance"').includes("shortcut-badge"), "receipts are not counted twice");
  env = await boot(me("admin"), { canReview: false });
  html = env.element("root").innerHTML;
  assert.ok(tile(html, 'data-path="/finance"').includes('shortcut-badge" aria-hidden="true">۴<'));
});

test("replying to a ticket re-reads /me so the support badge can drop", async () => {
  const env = await boot(me("user"));
  const before = env.calls.filter((c) => c.url === "/api/mini/me").length;
  const form = {
    getAttribute: (n) => (n === "data-id" ? "7" : n === "data-form" ? "ticket-reply" : null),
    elements: { body: { value: "thanks" } },
    querySelector: () => ({ disabled: false, isConnected: true }),
  };
  await env.mini.app.submitForm(form);
  await settle();
  assert.equal(env.calls.filter((c) => c.url === "/api/mini/me").length, before + 1);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); } catch (err) { console.error("FAILED:", name); throw err; }
  }
  console.log(`Mini App pull and badges: ${tests.length} checks passed`);
})().catch((err) => { console.error(err); process.exitCode = 1; });
