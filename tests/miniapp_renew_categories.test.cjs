"use strict";
// Mini App renewal: the service's own plan first, then the shop's categories, like the bot.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home" }, { id: "services" }, { id: "shop" }, { id: "account" }], user: { id: 1, name: "n", role: "user" },
  customer: { wallet: 900000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
    home: { service_count: 1, attention: [], recent: [] } },
};
const HOSTILE = "<img src=x onerror=alert(1)>";
const plan = (id, name, relation) => ({ id, name, price: 1000 * id, days: 30, gb: 10 * id, is_trial: false, relation });
const GROUPED = {
  wallet_pay_enabled: true,
  grouped: true,
  current: plan(1, "ماهانه", "current"),
  categories: [
    { id: 10, name: "اقتصادی", plans: [plan(1, "ماهانه", "current"), plan(2, "ماهانه کم", "downgrade")] },
    { id: 11, name: HOSTILE, plans: [plan(3, "سه‌ماهه", "upgrade")] },
  ],
  other: [plan(4, "بدون دسته", null)],
  plans: [],
};

async function boot(options) {
  const calls = [];
  const routes = async (method, url) => {
    calls.push(url);
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url === "/api/mini/service/7/renewal-options") return { status: 200, body: options };
    return { status: 404, body: { detail: "not found" } };
  };
  const env = loadMiniApp({ routes });
  env.mini.app.start();
  await settle();
  await env.mini.app.ACTIONS.renew(trigger({ "data-id": 7 }));
  await settle();
  return { env, calls, sheet: () => env.element("sheet-body").innerHTML };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the current plan comes first, then the categories and «سایر»", async () => {
  const { sheet } = await boot(GROUPED);
  const html = sheet();
  const current = html.indexOf('data-action="preview-renew" data-id="7" data-plan="1"');
  const firstCategory = html.indexOf('data-action="renew-category" data-id="7" data-cat="10"');
  assert.ok(current >= 0 && firstCategory > current, "the current plan is above the categories");
  assert.ok(html.includes('data-cat="11"') && html.includes('data-cat="none"'));
  assert.ok(html.includes("پلن فعلی"));
  assert.ok(!html.includes('data-plan="3"'), "other plans are reached through their category");
});

test("a category lists its plans with their relation, and back returns to the categories", async () => {
  const { env, sheet, calls } = await boot(GROUPED);
  env.mini.app.ACTIONS["renew-category"](trigger({ "data-id": 7, "data-cat": "10" }));
  assert.ok(sheet().includes('data-plan="1"') && sheet().includes('data-plan="2"'));
  assert.ok(sheet().includes("تنزل"));
  assert.ok(!sheet().includes('data-plan="3"'));
  env.mini.app.ACTIONS["renew-groups"](trigger({ "data-id": 7 }));
  assert.ok(sheet().includes('data-action="renew-category"'));
  assert.equal(calls.filter((u) => u.endsWith("/renewal-options")).length, 1, "the loaded choices are reused");
});

test("plans without a category are under «سایر»", async () => {
  const { env, sheet } = await boot(GROUPED);
  env.mini.app.ACTIONS["renew-category"](trigger({ "data-id": 7, "data-cat": "none" }));
  assert.ok(sheet().includes('data-plan="4"'));
});

test("a category name is escaped", async () => {
  const { env, sheet } = await boot(GROUPED);
  assert.ok(sheet().includes("&lt;img") && !sheet().includes("<img src=x"));
  env.mini.app.ACTIONS["renew-category"](trigger({ "data-id": 7, "data-cat": "11" }));
  assert.ok(sheet().includes("&lt;img") && !sheet().includes("<img src=x"));
});

test("an unknown category reloads the choices instead of showing an empty list", async () => {
  const { env, sheet, calls } = await boot(GROUPED);
  await env.mini.app.ACTIONS["renew-category"](trigger({ "data-id": 7, "data-cat": "99" }));
  await settle();
  assert.equal(calls.filter((u) => u.endsWith("/renewal-options")).length, 2);
  assert.ok(sheet().includes('data-action="renew-category"'));
});

test("a shop without categories keeps one list, current plan first", async () => {
  const { sheet } = await boot({ wallet_pay_enabled: true, grouped: false, current: null, categories: [], other: [],
    plans: [plan(1, "ماهانه", "current"), plan(2, "ماهانه کم", "downgrade")] });
  assert.ok(!sheet().includes('data-action="renew-category"'));
  assert.ok(sheet().indexOf('data-plan="1"') < sheet().indexOf('data-plan="2"'));
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
  console.log(`Mini App renewal categories: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
