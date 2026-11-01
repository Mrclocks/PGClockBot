"use strict";
// Mini App behavior: launch cost, escaping, double taps, lost responses, back button, link safety.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";

function me(persona, extra) {
  const commerce = persona !== "admin";
  const nav = persona === "admin"
    ? [{ id: "home", label: "داشبورد", icon: "home" }, { id: "ops", label: "میانبرها", icon: "ops" }]
    : [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" },
      { id: "shop", label: "خرید", icon: "shop" }, { id: "account", label: "حساب", icon: "account" }];
  return {
    persona, nav, commerce_allowed: commerce, currency: "تومان", panel_base: "https://panel.example.com",
    user: { id: 1, name: HOSTILE, role: persona },
    customer: commerce
      ? { wallet: 500000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
        home: { service_count: 1, attention: [], recent: [{ id: 7, username: HOSTILE, status: "active", status_fa: "فعال", remaining_label: "۵ روز" }] } }
      : { wallet: 0, wallet_pay_enabled: false, qr_enabled: false, commerce_allowed: false, home: { service_count: 0, attention: [], recent: [] } },
    ...(extra || {}),
  };
}

function service(id, extra) {
  return { id, username: "svc" + id, status: "active", status_fa: "فعال", traffic: "1 از 10 گیگ", traffic_pct: 10,
    expire_days_label: "۵ روز", subscription_url: "https://sub.example.com/sub/secret-token", addons_allowed: true,
    is_cancelled: false, cancellation_pending: false, ...(extra || {}) };
}

const PLANS = { wallet_pay_enabled: true, plans: [
  { id: 1, name: "trial", price: 0, is_trial: true },
  { id: 2, name: HOSTILE, price: 1250000, days: 30, gb: 50, is_trial: false },
] };
const PREVIEW = { terms: { version: 1 }, price: 200, request_key: "a".repeat(32), plan_name: HOSTILE,
  lines: ["حجم پس از تمدید: ۱۷ گیگ"], warnings: [], notice: "مصرف تا اجرا کم می‌شود" };

function router(overrides) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) {
      if (pattern.test(method + " " + url)) return handler(body, url);
    }
    if (url === "/api/mini/me") return { status: 200, body: me("user") };
    if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [service(7)], next_before_id: null } };
    const detail = /^\/api\/mini\/service\/(\d+)$/.exec(url);
    if (detail) return { status: 200, body: { service: service(Number(detail[1])) } };
    if (url === "/api/mini/plans") return { status: 200, body: PLANS };
    if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
    return { status: 404, body: { detail: "not found" } };
  };
}

async function boot(options) {
  const env = loadMiniApp(options);
  env.mini.app.start();
  await settle();
  return env;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("service times and usage read in Persian digits and calendar", async () => {
  const detail = service(7, { traffic: "27.9 از 30 گیگ", expire_days_label: "3 روز", expire: "2026/10/26 · 3 روز",
    expire_at: "2026-10-26T12:00:00+00:00", online_at: "2026-10-24T08:00:00+00:00" });
  const env = await boot({ routes: router([[/GET \/api\/mini\/service\/7$/, () => ({ status: 200, body: { service: detail } })]]) });
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(html.includes("۲۷٫۹ از ۳۰ گیگ") && html.includes("۳ روز"));
  assert.ok(!/2026|منقضی/.test(html), "no Gregorian date and no 'expired' on the last connection");
  assert.ok(html.includes("۱۴۰۵"), "dates use the Persian calendar");
});

test("Telegram's header and overscroll use the page colour", async () => {
  const env = await boot({ routes: router() });
  assert.deepEqual(env.context.Telegram.WebApp.colors, { header: "secondary_bg_color", background: "secondary_bg_color" });
});

test("Telegram's top insets apply only while the app is fullscreen", async () => {
  const env = await boot({ routes: router() });
  const tg = env.context.Telegram.WebApp;
  const attrs = env.context.document.documentElement.attrs;
  assert.equal(attrs["data-fullscreen"], "0");
  tg.isFullscreen = true;
  tg.events.fullscreenChanged();
  assert.equal(attrs["data-fullscreen"], "1");
  tg.isFullscreen = false;
  tg.events.fullscreenChanged();
  assert.equal(attrs["data-fullscreen"], "0");
});

test("launch reads only /me and escapes customer text", async () => {
  const env = await boot({ routes: router() });
  assert.deepEqual(env.calls.map((c) => c.url), ["/api/mini/me"]);
  assert.equal(env.calls[0].headers["X-Telegram-Init-Data"], "query_id=x&hash=y");
  const html = env.element("root").innerHTML;
  assert.ok(html.includes("&lt;img src=x"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(html.includes("۵۰۰,۰۰۰ تومان"));
  assert.ok(env.element("nav").innerHTML.includes('aria-current="page"'));
});

test("services list pages with before_id and searches by name", async () => {
  let page = 0;
  const env = await boot({ routes: router([[/GET \/api\/mini\/services/, () => {
    page += 1;
    return { status: 200, body: page === 1 ? { items: [service(9), service(8)], next_before_id: 8 } : { items: [service(3)], next_before_id: null } };
  }]]) });
  env.mini.app.go("services");
  await settle();
  env.mini.app.ACTIONS["services-more"](trigger({}));
  await settle();
  const urls = env.calls.map((c) => c.url).filter((u) => u.startsWith("/api/mini/services"));
  assert.deepEqual(urls, ["/api/mini/services", "/api/mini/services?before_id=8"]);
  assert.equal(env.mini.app.state.services.items.length, 3);
  env.mini.app.ACTIONS["services-more"](trigger({}));
  await settle();
  assert.equal(env.calls.filter((c) => c.url.startsWith("/api/mini/services")).length, 2, "no request past the last page");
});

test("cancelled service offers no renew or add-on actions", async () => {
  const env = await boot({ routes: router([[/GET \/api\/mini\/service\/5$/, () => ({ status: 200, body: { service: service(5, { is_cancelled: true }) } })]]) });
  env.mini.app.go("service", { serviceId: 5 });
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(!html.includes('data-action="renew"'));
  assert.ok(!html.includes('data-action="addons"'));
  assert.ok(html.includes('data-action="cancellation" data-id="5"'));
  assert.ok(!html.includes("secret-token"), "the subscription token is never shown on screen");
});

test("renewal preview is escaped and a lost response retries with the same request key", async () => {
  let lose = true;
  let release = null;
  const env = await boot({ routes: router([
    [/GET \/api\/mini\/service\/7\/renewal-preview/, () => ({ status: 200, body: PREVIEW })],
    [/POST \/api\/mini\/renew/, async () => {
      if (lose) throw new Error("response lost");
      if (release) await release;
      return { status: 200, body: { message: "تمدید شد" } };
    }],
  ]) });
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  env.mini.app.ACTIONS.renew(trigger({ "data-id": 7 }));
  await settle();
  env.mini.app.ACTIONS["preview-renew"](trigger({ "data-id": 7, "data-plan": 2 }));
  await settle();
  const sheet = env.element("sheet-body").innerHTML;
  assert.ok(sheet.includes("۱۷ گیگ"));
  assert.ok(sheet.includes('data-action="confirm-renew"'));
  assert.ok(sheet.includes("&lt;img") && !sheet.includes("<img src=x"));

  await env.mini.app.ACTIONS["confirm-renew"](trigger({ "data-id": 7, "data-plan": 2 }));
  assert.equal(env.element("sheet").hidden, false, "the preview stays open after a lost response");
  lose = false;
  let open;
  release = new Promise((resolve) => { open = resolve; });
  const confirm = trigger({ "data-id": 7, "data-plan": 2 });
  const first = env.mini.app.ACTIONS["confirm-renew"](confirm);
  const second = env.mini.app.ACTIONS["confirm-renew"](confirm);
  open();
  await Promise.all([first, second]);
  await settle();
  const posts = env.calls.filter((c) => c.method === "POST");
  assert.equal(posts.length, 2, "one lost try plus one retry; the double tap is ignored");
  assert.deepEqual(posts[0].body, posts[1].body);
  assert.equal(posts[1].body.preview.request_key, PREVIEW.request_key);
  await env.mini.app.ACTIONS["confirm-renew"](trigger({ "data-id": 7, "data-plan": 2 }));
  assert.equal(env.calls.filter((c) => c.method === "POST").length, 2, "a finished preview cannot be paid again");
});

test("renew sheet shares a plans request that is already loading", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot({ routes: router([[/GET \/api\/mini\/plans/, async () => { await pending; return { status: 200, body: PLANS }; }]]) });
  env.mini.app.go("shop");
  const sheet = env.mini.app.ACTIONS.renew(trigger({ "data-id": 7 }));
  open();
  await sheet;
  await settle();
  assert.equal(env.calls.filter((c) => c.url === "/api/mini/plans").length, 1);
  const html = env.element("sheet-body").innerHTML;
  assert.ok(html.includes('data-action="preview-renew"'));
  assert.ok(!html.includes('data-plan="1"'), "the trial plan is never offered for renewal");
});

test("a preview that arrives after its sheet closed is dropped", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot({ routes: router([[/renewal-preview/, async () => { await pending; return { status: 200, body: PREVIEW }; }]]) });
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  env.mini.app.ACTIONS.renew(trigger({ "data-id": 7 }));
  await settle();
  const stale = env.mini.app.ACTIONS["preview-renew"](trigger({ "data-id": 7, "data-plan": 2 }));
  env.mini.app.closeSheet();
  open();
  await stale;
  await settle();
  assert.equal(env.element("sheet").hidden, true);
  assert.equal(env.element("sheet-body").innerHTML, "");
});

test("a QR code that arrives after its sheet closed is dropped", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot({ routes: router([[/\/qr$/, async () => { await pending; return { status: 200, body: { png_base64: "iVBORw0KGgo=" } }; }]]) });
  const loading = env.mini.app.ACTIONS["show-qr"](trigger({ "data-id": 7 }));
  env.mini.app.closeSheet();
  open();
  await loading;
  await settle();
  assert.equal(env.element("sheet-body").innerHTML, "");
  assert.equal(env.element("sheet").hidden, true);
});

test("cancellation request is sent once and operator notes are escaped", async () => {
  let row = null;
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot({ routes: router([
    [/GET \/api\/mini\/service\/7\/cancellation/, () => ({ status: 200, body: { request: row } })],
    [/POST \/api\/mini\/service\/7\/cancellation/, async () => {
      await pending;
      row = { status: "pending", label: "در انتظار بررسی", operator_note: "<svg onload=alert(1)>", refund_amount: null };
      return { status: 200, body: { request: row } };
    }],
  ]) });
  env.mini.app.ACTIONS.cancellation(trigger({ "data-id": 7 }));
  await settle();
  assert.ok(env.element("sheet-body").innerHTML.includes('data-form="cancellation"'));
  const button = { disabled: false, isConnected: true };
  const form = {
    elements: { reason: { value: "دلیل مشتری" } },
    getAttribute: (name) => ({ "data-id": "7", "data-form": "cancellation" }[name] || null),
    querySelector: () => button,
  };
  const first = env.mini.app.submitForm(form);
  const second = env.mini.app.submitForm(form);
  open();
  await Promise.all([first, second]);
  await settle();
  assert.equal(env.calls.filter((c) => c.method === "POST").length, 1);
  const html = env.element("sheet-body").innerHTML;
  assert.ok(html.includes("&lt;svg"));
  assert.ok(!html.includes("<svg onload"));
  assert.ok(!html.includes('data-form="cancellation"'), "a pending request cannot be filed again");
});

test("purchase with a lost response is never retried automatically", async () => {
  let buys = 0;
  const env = await boot({ routes: router([[/POST \/api\/mini\/buy/, () => { buys += 1; throw new Error("lost"); }]]) });
  env.mini.app.go("shop");
  await settle();
  const buy = trigger({ "data-id": 2 });
  await Promise.all([env.mini.app.ACTIONS.buy(buy), env.mini.app.ACTIONS.buy(buy)]);
  await settle();
  assert.equal(buys, 1);
  const posts = env.calls.filter((c) => c.method === "POST");
  assert.deepEqual(posts[0].body, { plan_id: 2, price: 1250000 });
  assert.ok(env.calls.filter((c) => c.url === "/api/mini/me").length >= 2, "wallet is re-read after an unknown outcome");
  assert.equal(env.mini.app.state.view, "services");
});

test("declined confirmation sends nothing", async () => {
  const env = await boot({ routes: router(), confirm: () => false, telegram: true });
  env.context.Telegram.WebApp.showConfirm = (_msg, cb) => cb(false);
  env.mini.app.go("shop");
  await settle();
  await env.mini.app.ACTIONS.buy(trigger({ "data-id": 2 }));
  assert.equal(env.calls.filter((c) => c.method === "POST").length, 0);
});

test("admin persona has no commerce screens or purchases", async () => {
  const env = await boot({ routes: router([
    [/GET \/api\/mini\/me/, () => ({ status: 200, body: me("admin") })],
    [/GET \/api\/mini\/ops/, () => ({ status: 200, body: { stats: { pending: 2, tickets: 0, revenue: 10 }, panel_base: "https://panel.example.com", panel_links: [{ id: "finance", label: "مالی", path: "/finance" }] } })],
  ]) });
  assert.ok(!env.element("nav").innerHTML.includes('data-view="shop"'));
  env.mini.app.go("shop");
  assert.equal(env.mini.app.state.view, "home");
  env.mini.app.go("service", { serviceId: 7 });
  assert.equal(env.mini.app.state.view, "home");
  await env.mini.app.ACTIONS.buy(trigger({ "data-id": 2 }));
  assert.equal(env.calls.filter((c) => c.method === "POST").length, 0);
  await settle();
  assert.ok(env.element("root").innerHTML.includes('data-path="/finance"'));
});

test("panel links stay on the panel host", async () => {
  const env = await boot({ routes: router() });
  for (const path of ["//evil.example", "https://evil.example/x", "javascript:alert(1)", "/\\evil", "finance"]) {
    env.mini.app.ACTIONS["open-panel"](trigger({ "data-path": path }));
  }
  assert.equal(env.calls.filter((c) => c.method === "OPEN").length, 0);
  env.mini.app.ACTIONS["open-panel"](trigger({ "data-path": "/finance" }));
  assert.deepEqual(env.calls.filter((c) => c.method === "OPEN").map((c) => c.url), ["https://panel.example.com/finance"]);
});

test("Telegram back closes the sheet, then leaves the detail, then returns home", async () => {
  const env = await boot({ routes: router() });
  assert.equal(env.back.visible, false);
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  assert.equal(env.back.visible, true);
  env.mini.app.ACTIONS["show-qr"](trigger({ "data-id": 7 }));
  assert.equal(env.element("sheet").hidden, false);
  env.back.handler();
  assert.equal(env.element("sheet").hidden, true);
  assert.equal(env.mini.app.state.view, "service");
  env.back.handler();
  assert.equal(env.mini.app.state.view, "services");
  env.back.handler();
  assert.equal(env.mini.app.state.view, "home");
  assert.equal(env.back.visible, false);
});

test("subscription links: only http(s) opens and copy flashes its label", async () => {
  const core = loadMiniApp({ routes: router() }).mini.core;
  assert.equal(core.safeUrl("javascript:alert(1)"), "");
  assert.equal(core.safeUrl("data:text/html,x"), "");
  assert.equal(core.safeUrl(" https://sub.example.com/a "), "https://sub.example.com/a");
  const btn = { textContent: "کپی لینک", dataset: {}, classList: { add() {}, remove() {} } };
  core.markCopied(btn);
  assert.equal(btn.textContent, "کپی لینک", "label restored after the flash");
});

test("on-hold and unknown services never read as unlimited", async () => {
  const { expireDaysLabel } = loadMiniApp({ routes: router() }).mini.render;
  assert.equal(expireDaysLabel({ status: "on_hold", expire_days: null }), "پس از اتصال");
  assert.equal(expireDaysLabel({ pending_start: true, status: "active" }), "پس از اتصال");
  assert.equal(expireDaysLabel({ status: "active", error: "timeout" }), "—");
  assert.equal(expireDaysLabel({ status: "—" }), "—");
  assert.equal(expireDaysLabel({ status: "active", expire_days_label: "۳ روز" }), "۳ روز");
});

test("server errors in English are not shown to the customer", async () => {
  const env = await boot({ routes: router([[/GET \/api\/mini\/plans/, () => ({ status: 500, body: { detail: "Traceback: sqlalchemy error at 10.0.0.5" } })]]) });
  env.mini.app.go("shop");
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(!html.includes("sqlalchemy"));
  assert.ok(html.includes("دوباره تلاش کنید"));
});

test("launch outside Telegram explains how to open the app", async () => {
  const env = await boot({ routes: router(), telegram: false });
  assert.equal(env.calls.length, 0);
  assert.ok(env.element("root").innerHTML.includes("Open"));
});

const EXPIRED_TEXT = "نشست مینی‌اپ منقضی شده؛ مینی‌اپ را ببندید و دوباره باز کنید";

function expired() {
  return { status: 401, body: { detail: EXPIRED_TEXT }, headers: { "X-Mini-Error": "expired" } };
}

test("an expired session at launch offers closing the app, not a retry that cannot work", async () => {
  const env = await boot({ routes: router([[/GET \/api\/mini\/me$/, expired]]) });
  const html = env.element("root").innerHTML;
  assert.ok(html.includes(EXPIRED_TEXT));
  assert.ok(html.includes('data-action="close-app"'));
  assert.ok(!html.includes('data-action="reload"'));
  assert.ok(html.includes(">expired<"));
  env.mini.app.ACTIONS["close-app"](trigger({}));
  assert.ok(env.calls.some((c) => c.method === "CLOSE"));
});

test("a session that expires under an open screen replaces it with the reopen notice", async () => {
  const env = await boot({ routes: router([[/GET \/api\/mini\/plans/, expired]]) });
  assert.ok(env.element("nav").innerHTML.length > 0);
  env.mini.app.go("shop");
  await settle();
  assert.ok(env.element("root").innerHTML.includes('data-action="close-app"'));
  assert.equal(env.element("nav").innerHTML, "");
});

test("a refusal the server explains in Persian is shown as is, with a retry", async () => {
  const off = "مینی‌اپ این فروشگاه فعال نیست";
  const env = await boot({ routes: router([[/GET \/api\/mini\/me$/, () => ({ status: 404, body: { detail: off } })]]) });
  const html = env.element("root").innerHTML;
  assert.ok(html.includes(off));
  assert.ok(!html.includes("/start"));
  assert.ok(html.includes('data-action="reload"'));
});

test("a reason header that is not a short code is never rendered", async () => {
  const env = await boot({ routes: router([[/GET \/api\/mini\/me$/, () => ({
    status: 401, body: { detail: EXPIRED_TEXT }, headers: { "X-Mini-Error": HOSTILE },
  })]]) });
  const html = env.element("root").innerHTML;
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes('data-action="close-app"'));
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
  console.log(`Mini App behavior: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
