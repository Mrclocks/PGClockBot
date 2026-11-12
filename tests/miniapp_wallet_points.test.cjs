"use strict";
// Wallet card: points + باشگاه shortcut only while loyalty is enabled for that customer payload.
const assert = require("node:assert/strict");
const { loadMiniApp, settle } = require("./miniapp_env.cjs");

function me(customerExtra) {
  return {
    persona: "user",
    commerce_allowed: true,
    currency: "تومان",
    panel_base: "",
    nav: [
      { id: "home", label: "خانه", icon: "home" },
      { id: "account", label: "حساب", icon: "account" },
    ],
    sections: ["services", "guides", "activity", "shop", "topup", "support", "codes", "club"],
    user: { id: 1, name: "a" },
    customer: {
      wallet: 50000,
      wallet_pay_enabled: true,
      qr_enabled: true,
      commerce_allowed: true,
      home: { service_count: 0, attention: [], recent: [] },
      ...(customerExtra || {}),
    },
  };
}

function router(customerExtra) {
  return async (_method, url) => {
    if (url === "/api/mini/me") return { status: 200, body: me(customerExtra) };
    if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
    if (url === "/api/mini/topup") return { status: 200, body: { recent: [] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function accountHtml(customerExtra) {
  const env = loadMiniApp({ routes: router(customerExtra) });
  env.mini.app.start();
  await settle();
  env.mini.app.go("account");
  await settle();
  return env.element("root").innerHTML;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function walletCardChunk(html) {
  const start = html.indexOf('aria-label="کیف پول"');
  assert.ok(start >= 0, "wallet hero present");
  const end = html.indexOf("</section>", start);
  return html.slice(start, end + "</section>".length);
}

test("wallet card shows formatted points and a club shortcut when loyalty is on", async () => {
  const card = walletCardChunk(await accountHtml({ loyalty_enabled: true, points: 1500 }));
  assert.ok(card.includes("امتیاز"), "points label on the wallet card");
  assert.ok(card.includes("۱٬۵۰۰"), "Persian-grouped points");
  assert.ok(card.includes('data-view="club"') && card.includes("باشگاه"), "club shortcut");
});

test("wallet card stays unchanged when loyalty is off", async () => {
  const card = walletCardChunk(await accountHtml({}));
  assert.ok(card.includes("موجودی کیف پول"));
  assert.ok(!card.includes("امتیاز"), "no points line on the wallet card");
  assert.ok(!card.includes("باشگاه"), "no club shortcut on the wallet card");
});

test("club shortcut on the wallet card is hidden when the shop Mini App left club closed", async () => {
  const body = me({ loyalty_enabled: true, points: 10 });
  body.sections = ["services", "guides", "activity", "shop", "topup", "support", "codes"];
  const env = loadMiniApp({
    routes: async (_method, url) => {
      if (url === "/api/mini/me") return { status: 200, body };
      if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
      if (url === "/api/mini/topup") return { status: 200, body: { recent: [] } };
      return { status: 404, body: { detail: "x" } };
    },
  });
  env.mini.app.start();
  await settle();
  env.mini.app.go("account");
  await settle();
  const card = walletCardChunk(env.element("root").innerHTML);
  assert.ok(card.includes("امتیاز") && card.includes("۱۰"), "points still show");
  assert.ok(!card.includes("باشگاه"), "no club button without the club section");
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log("ok - " + name);
    } catch (err) {
      failed += 1;
      console.error("not ok - " + name);
      console.error(err);
    }
  }
  if (failed) process.exit(1);
})();
