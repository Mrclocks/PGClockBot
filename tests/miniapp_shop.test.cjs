"use strict";
// A shop's Mini App sends the key from its own address; the platform Mini App sends none.
const assert = require("node:assert/strict");
const { loadMiniApp, settle } = require("./miniapp_env.cjs");

const KEY = "Ab3_-xY9Ab3_-xY9Ab3_-x";
const routes = async () => ({ status: 404, body: { detail: "پیدا نشد" } });

async function shopHeaders(pathname) {
  const env = loadMiniApp({ routes, pathname });
  env.mini.app.start();
  await settle();
  assert.ok(env.calls.length > 0, "the app asked the server for /me");
  return env.calls.map((call) => call.headers["X-Mini-Shop"]);
}

const SHOP_ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  sections: ["services", "guides", "activity", "shop"], shop: { name: "<b>shop</b>" },
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "services", label: "سرویس‌ها", icon: "svc" },
    { id: "shop", label: "خرید", icon: "shop" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a", wallet: 7000 },
  customer: { wallet: 7000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
    home: { service_count: 0, attention: [], recent: [] } },
};
const SERVICE = { id: 7, username: "svc", status: "active", status_fa: "فعال", traffic_pct: 10, addons_allowed: true,
  subscription_url: "https://sub.example.com/sub/token", is_cancelled: false, cancellation_pending: false };

async function shopScreensHideUnopenedSections() {
  const shopRoutes = async (_method, url) => {
    if (url === "/api/mini/me") return { status: 200, body: SHOP_ME };
    if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
    if (url === "/api/mini/service/7") return { status: 200, body: { service: SERVICE } };
    if (url === "/api/mini/plans") return { status: 200, body: { wallet_pay_enabled: true, plans: [{ id: 3, name: "big", price: 90000, days: 30, gb: 50, is_trial: false }] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
  const env = loadMiniApp({ routes: shopRoutes, pathname: `/miniapp/s/${KEY}/` });
  env.mini.app.start();
  await settle();
  let html = env.element("root").innerHTML;
  assert.ok(html.includes("&lt;b&gt;shop&lt;/b&gt;"), "shop name is escaped");
  assert.ok(!html.includes('data-action="topup"'), "no top-up on home");
  assert.ok(html.includes('data-view="shop"'), "buying is open");
  env.mini.app.go("account");
  await settle();
  html = env.element("root").innerHTML;
  for (const hidden of ['data-action="topup"', 'data-view="support"', 'data-view="club"', 'data-action="gift-code"']) {
    assert.ok(!html.includes(hidden), `account hides ${hidden}`);
  }
  assert.ok(!env.calls.some((c) => c.url === "/api/mini/topup"), "closed sections are never requested");
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  html = env.element("root").innerHTML;
  assert.ok(html.includes('data-action="renew"'), "renewal is open");
  for (const hidden of ["automation", "cancellation"]) {
    assert.ok(!html.includes(`data-action="${hidden}"`), `service hides ${hidden}`);
  }
  assert.ok(html.includes('data-action="copy-link"') && html.includes('data-action="guides"'));
  env.mini.app.go("shop");
  await settle();
  env.mini.app.go("shop");
  await settle();
  html = env.element("root").innerHTML;
  assert.ok(html.includes("big"), "shop plans are listed");
  assert.ok(html.includes("موجودی کیف پول کافی نیست"), "the checkout shows the shortfall");
  assert.ok(!html.includes('data-form="discount"'), "no discount code field before codes open");
  assert.ok(!html.includes('data-action="topup"'), "no top-up button before top-up opens");
  env.mini.app.go("support");
  await settle();
  assert.ok(!env.calls.some((c) => c.url.startsWith("/api/mini/support")), "support screen does not open");
}

async function shopTopupOpensWithItsSection() {
  const me = { ...SHOP_ME, sections: [...SHOP_ME.sections, "topup"] };
  const topupRoutes = async (_method, url) => {
    if (url === "/api/mini/me") return { status: 200, body: me };
    if (url === "/api/mini/topup") {
      return { status: 200, body: { methods: [{ key: "card", label: "کارت", needs_receipt: true, choices: [] }],
        presets: [50000], min_amount: 1000, max_amount: 1000000, bot_only: false, recent: [] } };
    }
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
  const env = loadMiniApp({ routes: topupRoutes, pathname: `/miniapp/s/${KEY}/` });
  env.mini.app.start();
  await settle();
  env.mini.app.go("account");
  await settle();
  assert.ok(env.element("root").innerHTML.includes('data-action="topup"'), "the account offers a top-up");
  const call = env.calls.find((c) => c.url === "/api/mini/topup");
  assert.ok(call, "the shop's top-up options are requested");
  assert.equal(call.headers["X-Mini-Shop"], KEY, "with the shop key");
}

async function shopExtrasOpenWithTheirSections() {
  const me = { ...SHOP_ME, sections: [...SHOP_ME.sections, "support", "codes", "club"] };
  const env = loadMiniApp({
    routes: async (_method, url) => (url === "/api/mini/me" ? { status: 200, body: me } : { status: 404, body: { detail: "x" } }),
    pathname: `/miniapp/s/${KEY}/`,
  });
  env.mini.app.start();
  await settle();
  env.mini.app.go("account");
  await settle();
  const html = env.element("root").innerHTML;
  for (const shown of ['data-view="support"', 'data-view="club"']) {
    assert.ok(html.includes(shown), `account offers ${shown}`);
  }
  env.mini.app.go("support");
  await settle();
  const call = env.calls.find((c) => c.url === "/api/mini/support");
  assert.ok(call && call.headers["X-Mini-Shop"] === KEY, "support is asked for with the shop key");
}

(async () => {
  assert.deepEqual([...new Set(await shopHeaders("/miniapp/"))], [undefined], "platform page sends no shop");
  assert.deepEqual([...new Set(await shopHeaders(`/miniapp/s/${KEY}/`))], [KEY]);
  assert.deepEqual([...new Set(await shopHeaders(`/miniapp/s/${KEY}`))], [KEY]);
  for (const bad of ["/miniapp/s/short/", `/miniapp/s/${KEY}/extra`, `/miniapp/s/${KEY}%3Cx/`, "/other/s/" + KEY + "/"]) {
    assert.deepEqual([...new Set(await shopHeaders(bad))], [undefined], `no key from ${bad}`);
  }
  await shopScreensHideUnopenedSections();
  await shopTopupOpensWithItsSection();
  await shopExtrasOpenWithTheirSections();
  console.log("miniapp shop ok");
})().catch((err) => { console.error(err); process.exit(1); });
