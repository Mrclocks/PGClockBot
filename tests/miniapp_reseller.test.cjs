"use strict";
// Mini App «اشتراک نمایندگی»: escaping, one purchase per tap, the generation sent, role gating.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";

function me(persona) {
  return {
    persona, commerce_allowed: true, currency: "تومان", panel_base: "",
    nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" },
      { id: "shop", label: "خرید", icon: "shop" }, { id: "account", label: "حساب", icon: "account" }],
    user: { id: 1, name: "n", role: persona },
    customer: { wallet: 900000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
      home: { service_count: 0, attention: [], recent: [] } },
  };
}

function subscription(extra) {
  return {
    wallet: 900000, plan_name: HOSTILE, is_payg: false, status: "active", expires_at: null, days_left: 12,
    base_gb: 100, extra_gb: 20, base_users: 10, extra_users: 0, generation: 4, usage: { users: 3, used_gb: 40 },
    renew_total: 500000, renew_days: 30, unavailable: null,
    packs: [{ id: 9, name: HOSTILE, description: "", gb: 200, users: 0, price: 250000 }],
    unit_gb: { unit_price: 1000, options: [1, 5, 10, 50] }, unit_users: null, ...(extra || {}),
  };
}

function boot(persona, subs, posts) {
  const routes = async (method, url, body) => {
    if (url === "/api/mini/me") return { status: 200, body: me(persona) };
    if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [], next_before_id: null } };
    if (url === "/api/mini/reseller/subscription") return { status: 200, body: subs() };
    if (method === "POST" && url.startsWith("/api/mini/reseller/")) {
      posts.push({ url, body });
      return { status: 200, body: { ok: true, message: "ok", wallet: 1 } };
    }
    return { status: 404, body: { detail: "not found" } };
  };
  const env = loadMiniApp({ routes });
  env.mini.app.start();
  return settle().then(() => env);
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the subscription screen escapes pack and plan names", async () => {
  const env = await boot("reseller", () => subscription(), []);
  env.mini.app.go("subscription");
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(!html.includes("<img"), "hostile names are escaped");
  assert.ok(html.includes('data-action="res-pack-buy"') && html.includes("+۲۰۰ گیگ"));
  assert.ok(html.includes('data-action="res-renew"'));
});

test("a pack purchase is sent once with the generation of the loaded screen", async () => {
  const posts = [];
  const env = await boot("reseller", () => subscription(), posts);
  env.mini.app.go("subscription");
  await settle();
  const tap = trigger({ "data-pack": 9 });
  await Promise.all([env.mini.app.ACTIONS["res-pack-buy"](tap), env.mini.app.ACTIONS["res-pack-buy"](tap)]);
  assert.equal(posts.length, 1, "the second tap is ignored while the first runs");
  assert.deepEqual(posts[0], { url: "/api/mini/reseller/pack", body: { pack_id: 9, generation: 4 } });
});

test("a pack that is not on the screen is never sent", async () => {
  const posts = [];
  const env = await boot("reseller", () => subscription(), posts);
  env.mini.app.go("subscription");
  await settle();
  await env.mini.app.ACTIONS["res-pack-buy"](trigger({ "data-pack": 77 }));
  await env.mini.app.ACTIONS["res-extra-buy"](trigger({ "data-kind": "gb", "data-amount": 7 }));
  await env.mini.app.ACTIONS["res-extra-buy"](trigger({ "data-kind": "users", "data-amount": 5 }));
  assert.equal(posts.length, 0);
});

test("a declined confirmation buys nothing", async () => {
  const posts = [];
  const routes = async (method, url, body) => {
    if (url === "/api/mini/me") return { status: 200, body: me("reseller") };
    if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [], next_before_id: null } };
    if (url === "/api/mini/reseller/subscription") return { status: 200, body: subscription() };
    if (method === "POST") posts.push({ url, body });
    return { status: 404, body: { detail: "not found" } };
  };
  const env = loadMiniApp({ routes, confirm: () => false });
  env.mini.app.start();
  await settle();
  env.mini.app.go("subscription");
  await settle();
  await env.mini.app.ACTIONS["res-renew"](trigger({}));
  assert.equal(posts.length, 0);
});

test("a plan without time shows why there is no renewal and offers no renew button", async () => {
  const note = "این پلن زمان ندارد";
  const env = await boot("reseller", () => subscription({ renew_total: null, renew_note: note }), []);
  env.mini.app.go("subscription");
  await settle();
  const html = env.element("root").innerHTML;
  assert.ok(html.includes(note) && !html.includes('data-action="res-renew"'));
});

test("the renew button says customers' usage is untouched", async () => {
  const env = await boot("reseller", () => subscription(), []);
  env.mini.app.go("subscription");
  await settle();
  assert.ok(env.element("root").innerHTML.includes("مصرف مشتریان شما دست‌نخورده"));
});

test("a customer cannot open the subscription screen", async () => {
  const env = await boot("user", () => subscription(), []);
  env.mini.app.go("subscription");
  await settle();
  assert.ok(!env.element("root").innerHTML.includes("res-pack-buy"));
  assert.ok(!env.calls.some((c) => c.url === "/api/mini/reseller/subscription"), "no request is made");
});

test("a reseller finds the package next to their services and on the home screen", async () => {
  const env = await boot("reseller", () => subscription(), []);
  assert.ok(env.element("root").innerHTML.includes('data-view="subscription"'), "home card");
  env.mini.app.go("services");
  await settle();
  assert.ok(env.element("root").innerHTML.includes('data-view="subscription"'), "services card");
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
  console.log(`Mini App reseller: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
