"use strict";
// Mini App wallet top-up: escaping, amount rules, single submit, safe links, receipt upload.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const OPTIONS = {
  methods: [
    { key: "card", label: HOSTILE, needs_receipt: true, choices: [{ id: "c1", label: "…1111 (" + HOSTILE + ")" }] },
    { key: "psp", label: "پرداخت آنلاین", needs_receipt: false, choices: [] },
  ],
  presets: [100000, 250000], min_amount: 1000, max_amount: 500000000, bot_only: true, recent: [],
};
const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 0, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};

function router(overrides) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) {
      if (pattern.test(method + " " + url)) return handler(body, url);
    }
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url === "/api/mini/topup") return { status: 200, body: OPTIONS };
    if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
    return { status: 404, body: { detail: "not found" } };
  };
}

async function boot(overrides) {
  const env = loadMiniApp({ routes: router(overrides) });
  env.mini.app.start();
  await settle();
  return env;
}

function topupForm({ preset = "100000", custom = "", method = "card", dest = "c1" } = {}) {
  const button = { disabled: false, isConnected: true };
  return {
    button,
    elements: {
      preset: { value: preset }, custom_amount: { value: custom }, method: { value: method },
      ...(dest ? { dest_card: { value: dest } } : {}),
    },
    getAttribute: (name) => ({ "data-form": "topup" }[name] || null),
    querySelector: () => button,
  };
}

function receiptForm(paymentId, file) {
  const button = { disabled: false, isConnected: true };
  return {
    elements: { receipt: { files: file ? [file] : [] } },
    getAttribute: (name) => ({ "data-form": "receipt", "data-id": String(paymentId) }[name] || null),
    querySelector: () => button,
  };
}

const posts = (env, part) => env.calls.filter((c) => c.method === "POST" && c.url.includes(part));
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("top-up sheet escapes admin labels and explains bot-only methods", async () => {
  const env = await boot();
  await env.mini.app.ACTIONS.topup(trigger({}));
  await settle();
  const html = env.element("sheet-body").innerHTML;
  assert.ok(html.includes("&lt;img src=x"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(html.includes('name="dest_card"'));
  assert.ok(html.includes("بزن به کارت"));
});

test("amount rules: presets, Persian digits, bounds; nothing is sent for a bad amount", async () => {
  const env = await boot([[/POST \/api\/mini\/topup$/, (body) => ({ status: 200, body: { topup: { payment_id: 9, method: "card", amount: body.amount, needs_receipt: true, card_number: "6037991111111111" } } })]]);
  await env.mini.app.ACTIONS.topup(trigger({}));
  await settle();
  for (const custom of ["", "999", "12.5", "abc", "۶۰۰٬۰۰۰٬۰۰۰"]) {
    await env.mini.app.submitForm(topupForm({ preset: "custom", custom }));
  }
  assert.equal(posts(env, "/api/mini/topup").length, 0);
  await env.mini.app.submitForm(topupForm({ preset: "custom", custom: "۱۵۰٬۰۰۰" }));
  await settle();
  assert.deepEqual(posts(env, "/api/mini/topup")[0].body, { amount: 150000, method: "card", dest_id: "c1" });
  const html = env.element("sheet-body").innerHTML;
  assert.ok(html.includes("6037991111111111") && html.includes('data-form="receipt"'));
});

test("a double tap on «ادامه» starts one payment", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  const env = await boot([[/POST \/api\/mini\/topup$/, async () => { await pending; return { status: 200, body: { topup: { payment_id: 9, method: "psp", amount: 100000, needs_receipt: false, checkout_url: "javascript:alert(1)", settlement_id: 3 } } }; }]]);
  await env.mini.app.ACTIONS.topup(trigger({}));
  await settle();
  const form = topupForm({ method: "psp", dest: null });
  const first = env.mini.app.submitForm(form);
  const second = env.mini.app.submitForm(form);
  open();
  await Promise.all([first, second]);
  await settle();
  assert.equal(posts(env, "/api/mini/topup").length, 1);
  env.mini.app.ACTIONS["open-topup-link"](trigger({}));
  assert.equal(env.calls.filter((c) => c.method === "OPEN").length, 0, "a non-http checkout link is never opened");
});

test("receipt upload is sent once, and a lost response is not retried", async () => {
  let open;
  const pending = new Promise((resolve) => { open = resolve; });
  let lose = false;
  const env = await boot([[/POST \/api\/mini\/topup\/9\/receipt/, async () => {
    await pending;
    if (lose) throw new Error("lost");
    return { status: 200, body: { status: "pending", message: "رسید ثبت شد" } };
  }]]);
  env.mini.app.ACTIONS["topup-receipt"](trigger({ "data-id": 9 }));
  const file = { type: "image/jpeg", size: 1000 };
  const form = receiptForm(9, file);
  const first = env.mini.app.submitForm(form);
  const second = env.mini.app.submitForm(form);
  open();
  await Promise.all([first, second]);
  await settle();
  const uploads = posts(env, "/receipt");
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].body, file, "the image is sent as the raw body");
  assert.equal(uploads[0].headers["Content-Type"], "image/jpeg");
  assert.equal(env.element("sheet").hidden, true);

  lose = true;
  const mesBefore = env.calls.filter((c) => c.url === "/api/mini/me").length;
  env.mini.app.ACTIONS["topup-receipt"](trigger({ "data-id": 9 }));
  await env.mini.app.submitForm(receiptForm(9, file));
  await settle();
  assert.equal(posts(env, "/receipt").length, 2, "one try, no automatic retry");
  assert.ok(env.calls.filter((c) => c.url === "/api/mini/me").length > mesBefore, "state is re-read after an unknown outcome");
});

test("non-image files are never uploaded", async () => {
  const env = await boot();
  env.mini.app.ACTIONS["topup-receipt"](trigger({ "data-id": 9 }));
  for (const file of [{ type: "application/pdf" }, { type: "image/svg+xml" }, null]) {
    await env.mini.app.submitForm(receiptForm(9, file));
  }
  assert.equal(posts(env, "/receipt").length, 0);
});

test("gateway check result is shown and a settled payment refreshes the wallet", async () => {
  const env = await boot([[/POST \/api\/mini\/topup\/check/, (body) => ({ status: 200, body: { outcome: "settled", message: "پرداخت تأیید شد", id: body.settlement_id } })]]);
  const mesBefore = env.calls.filter((c) => c.url === "/api/mini/me").length;
  await env.mini.app.ACTIONS["check-topup"](trigger({ "data-id": 3 }));
  await settle();
  assert.deepEqual(posts(env, "/check")[0].body, { settlement_id: 3 });
  assert.ok(env.calls.filter((c) => c.url === "/api/mini/me").length > mesBefore);
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
  console.log(`Mini App top-up: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
