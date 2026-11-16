"use strict";
// «افزودن به صفحهٔ اصلی» in the account tab: shown only while Telegram can add a shortcut,
// asks Telegram's own prompt, and confirms once Telegram reports the shortcut added.
const assert = require("node:assert/strict");
const { loadMiniApp, settle } = require("./miniapp_env.cjs");

const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 0, wallet_pay_enabled: false, qr_enabled: false, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};

const routes = async (_method, url) => {
  if (url === "/api/mini/me") return { status: 200, body: ME };
  if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
  return { status: 404, body: { detail: "پیدا نشد" } };
};

/** status: what Telegram answers to checkHomeScreenStatus; version: whether it is 8.0 or newer. */
async function account({ status = "missed", version = true, telegram = true } = {}) {
  // Without Telegram the sign-in comes from the address, as launch.js reads it.
  const launch = telegram ? null : { initData: "query_id=x&hash=y", inTelegram: true, sdk: Promise.resolve(false) };
  const env = loadMiniApp({ routes, telegram, launch });
  const tg = env.context.Telegram && env.context.Telegram.WebApp;
  const prompts = [];
  if (tg) {
    tg.isVersionAtLeast = () => version;
    tg.checkHomeScreenStatus = (callback) => { prompts.push("check"); callback(status); };
    tg.addToHomeScreen = () => prompts.push("add");
  }
  env.mini.app.start();
  await settle();
  env.mini.app.go("account");
  await settle();
  return { env, tg, prompts, html: () => env.element("root").innerHTML };
}

const ROW = 'data-action="add-home-screen"';
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the row shows while Telegram can still add a shortcut", async () => {
  for (const status of ["missed", "unknown"]) {
    const { html } = await account({ status });
    assert.ok(html().includes(ROW), status);
    assert.ok(html().includes("افزودن به صفحهٔ اصلی"), status);
  }
});

test("no row once added, when unsupported, on old Telegram apps or in a browser", async () => {
  for (const options of [{ status: "added" }, { status: "unsupported" }, { status: "bogus" }, { version: false }, { telegram: false }]) {
    const { html, prompts } = await account(options);
    assert.ok(!html().includes(ROW), JSON.stringify(options));
    if (options.version === false) assert.deepEqual(prompts, [], "an old app is never asked");
  }
});

test("tapping asks Telegram once, and «added» hides the row with a confirmation", async () => {
  const { env, tg, prompts, html } = await account();
  env.mini.app.ACTIONS["add-home-screen"]();
  assert.deepEqual(prompts, ["check", "add"]);
  tg.events.homeScreenAdded();
  await settle();
  assert.ok(!html().includes(ROW));
  assert.equal(env.toasts.length, 1);
  assert.equal(env.toasts[0].textContent, "به صفحهٔ اصلی اضافه شد");
});

test("a shortcut that already exists at launch is not announced", async () => {
  const { env, tg } = await account({ status: "missed" });
  tg.events.homeScreenChecked({ status: "added" });
  await settle();
  assert.equal(env.toasts.length, 0);
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
  console.log(`Mini App home screen: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
