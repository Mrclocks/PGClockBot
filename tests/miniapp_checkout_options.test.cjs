"use strict";
// Mini App checkout extras: the required service name and the staff free button.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const PLANS = { wallet_pay_enabled: true, plans: [{ id: 1, name: "a", price: 100000, days: 30, gb: 50, is_trial: false }] };

function me(checkout) {
  return {
    persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
    nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" }, { id: "shop", label: "خرید", icon: "shop" }],
    user: { id: 1, name: "a" },
    customer: {
      wallet: 500000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
      home: { service_count: 0, attention: [], recent: [] }, checkout,
    },
  };
}

function router(checkout, overrides) {
  return async (method, url, body) => {
    for (const [pattern, handler] of overrides || []) {
      if (pattern.test(method + " " + url)) return handler(body, url);
    }
    if (url === "/api/mini/me") return { status: 200, body: me(checkout) };
    if (url === "/api/mini/plans") return { status: 200, body: PLANS };
    if (url === "/api/mini/buy") return { status: 200, body: { ok: true, order_id: 9, wallet: 0, message: "ok" } };
    if (url.startsWith("/api/mini/")) return { status: 200, body: { items: [], recent: [] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function shop(checkout, overrides) {
  const env = loadMiniApp({ routes: router(checkout, overrides) });
  env.mini.app.start();
  await settle();
  env.mini.app.go("shop");
  await settle();
  return env;
}

const buys = (env) => env.calls.filter((c) => c.method === "POST" && c.url === "/api/mini/buy");
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("name rules match the server and the random name always passes them", () => {
  const { render } = loadMiniApp({ routes: router({}) }).mini;
  assert.equal(render.validServiceName("  @Ali_22 "), "ali_22");
  for (const bad of ["ab", "1ali", "علی", "ali-22", "a".repeat(21), ""]) assert.equal(render.validServiceName(bad), "", bad);
  let seed = 0;
  const rand = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
  for (let i = 0; i < 50; i += 1) {
    const name = render.randomServiceName(rand);
    assert.equal(render.validServiceName(name), name);
  }
});

test("the checkout card shows the name field and free button only when the server says so, escaped", () => {
  const { render } = loadMiniApp({ routes: router({}) }).mini;
  const plan = PLANS.plans[0];
  const plain = render.checkoutHtml(plan, 500000, {}, {});
  assert.ok(!plain.includes("service-name") && !plain.includes("buy-free"));
  const full = render.checkoutHtml(plan, 500000, {}, { nameRequired: true, staffFree: true, name: HOSTILE });
  assert.ok(full.includes('data-input="service-name"') && full.includes('data-action="buy-free"'));
  assert.ok(!full.includes(HOSTILE), "the typed name is escaped");
});

test("a required name blocks the purchase until it is valid, then is sent", async () => {
  const env = await shop({ service_name_required: true, staff_free: false });
  await env.mini.app.ACTIONS.buy(trigger({ "data-id": 1 }));
  assert.equal(buys(env).length, 0, "no name, no request");
  env.mini.app.state.serviceName = "علی";
  await env.mini.app.ACTIONS.buy(trigger({ "data-id": 1 }));
  assert.equal(buys(env).length, 0, "an invalid name is refused before the request");
  env.mini.app.state.serviceName = "Ali";
  await env.mini.app.ACTIONS.buy(trigger({ "data-id": 1 }));
  await settle();
  assert.deepEqual(buys(env)[0].body, { plan_id: 1, price: 100000, service_name: "ali" });
  assert.equal(env.mini.app.state.serviceName, "", "the name is cleared after the purchase");
});

test("without a required name nothing extra is sent", async () => {
  const env = await shop({ service_name_required: false, staff_free: false });
  await env.mini.app.ACTIONS.buy(trigger({ "data-id": 1 }));
  await settle();
  assert.deepEqual(buys(env)[0].body, { plan_id: 1, price: 100000 });
});

test("the random button fills a valid name", async () => {
  const env = await shop({ service_name_required: true });
  env.mini.app.ACTIONS["random-service-name"](trigger({}));
  assert.ok(env.mini.render.validServiceName(env.mini.app.state.serviceName));
});

test("staff free sends the free mode once per tap, and only for staff", async () => {
  const env = await shop({ service_name_required: false, staff_free: true });
  const t = trigger({ "data-id": 1 });
  await Promise.all([env.mini.app.ACTIONS["buy-free"](t), env.mini.app.ACTIONS["buy-free"](t)]);
  await settle();
  assert.equal(buys(env).length, 1);
  assert.deepEqual(buys(env)[0].body, { plan_id: 1, price: 100000, mode: "staff_free" });
  assert.equal(env.mini.app.state.view, "services");

  const customer = await shop({ service_name_required: false, staff_free: false });
  await customer.mini.app.ACTIONS["buy-free"](trigger({ "data-id": 1 }));
  assert.equal(buys(customer).length, 0);
});

test("the discounted checkout pays with the required name", async () => {
  const checkout = { order_id: 77, amount: 80000, original_amount: 100000, discount: 20000, code: "OFF" };
  const env = await shop({ service_name_required: true }, [
    [/POST \/api\/mini\/checkout$/, () => ({ status: 200, body: { checkout } })],
    [/POST \/api\/mini\/checkout\/77\/pay/, () => ({ status: 200, body: { ok: true, message: "ok" } })],
  ]);
  env.mini.app.state.checkout = { ...checkout, planId: 1 };
  await env.mini.app.ACTIONS["checkout-pay"](trigger({}));
  assert.equal(env.calls.filter((c) => /\/77\/pay$/.test(c.url)).length, 0);
  env.mini.app.state.serviceName = "ali";
  await env.mini.app.ACTIONS["checkout-pay"](trigger({}));
  await settle();
  const pay = env.calls.find((c) => /\/77\/pay$/.test(c.url));
  assert.deepEqual(pay.body, { service_name: "ali" });
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
  console.log(`Mini App checkout options: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
