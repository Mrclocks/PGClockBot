"use strict";
// Mini App service packs: the volume/time buttons open the list and a tapped pack is bought once.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home" }, { id: "services" }, { id: "shop" }, { id: "account" }], user: { id: 1, name: "n", role: "user" },
  customer: { wallet: 900000, wallet_pay_enabled: true, qr_enabled: true, commerce_allowed: true,
    home: { service_count: 1, attention: [], recent: [] } },
};
const SERVICE = { id: 7, username: "s7", status: "active", status_fa: "فعال", traffic: "1 از 10", traffic_pct: 10,
  expire_days_label: "5", subscription_url: "https://x.example/sub/t", addons_allowed: true, is_cancelled: false, cancellation_pending: false };

function pack(kind, id) {
  return { id, name: "p" + id, kind, amount_label: kind === "volume" ? "5 گیگ" : "7 روز", description: "", price: 800 };
}

async function boot(posts, { confirm = () => true, fail = false } = {}) {
  const routes = async (method, url, body) => {
    if (url === "/api/mini/me") return { status: 200, body: ME };
    if (url.startsWith("/api/mini/services")) return { status: 200, body: { items: [SERVICE], next_before_id: null } };
    if (url === "/api/mini/service/7") return { status: 200, body: { service: SERVICE } };
    const list = /^\/api\/mini\/service\/7\/addons\?kind=(\w+)$/.exec(url);
    if (list) return { status: 200, body: { kind: list[1], packs: [pack(list[1], list[1] === "volume" ? 1 : 2)] } };
    if (url === "/api/mini/addon") {
      posts.push(body);
      return fail ? { status: 400, body: { detail: "موجودی کافی نیست" } } : { status: 200, body: { ok: true, message: "انجام شد" } };
    }
    return { status: 404, body: { detail: "not found" } };
  };
  const env = loadMiniApp({ routes, confirm });
  env.mini.app.start();
  await settle();
  env.mini.app.go("service", { serviceId: 7 });
  await settle();
  return env;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("the volume and time buttons open their pack lists", async () => {
  const env = await boot([]);
  assert.ok(env.element("root").innerHTML.includes('data-kind="volume"') && env.element("root").innerHTML.includes('data-kind="duration"'));
  for (const kind of ["volume", "duration"]) {
    await env.mini.app.ACTIONS.addons(trigger({ "data-id": 7, "data-kind": kind }));
    await settle();
    assert.ok(env.element("sheet-body").innerHTML.includes('data-action="buy-addon"'), kind);
  }
});

test("a tapped pack is bought, once, with its own kind", async () => {
  for (const [kind, packId] of [["volume", 1], ["duration", 2]]) {
    const posts = [];
    const env = await boot(posts);
    await env.mini.app.ACTIONS.addons(trigger({ "data-id": 7, "data-kind": kind }));
    await settle();
    const tap = trigger({ "data-id": 7, "data-pack": packId });
    await Promise.all([env.mini.app.ACTIONS["buy-addon"](tap), env.mini.app.ACTIONS["buy-addon"](tap)]);
    await settle();
    assert.equal(posts.length, 1, kind + ": the second tap is ignored while the first runs");
    assert.deepEqual(posts[0], { service_id: 7, pack_id: packId, kind, price: 800 });
  }
});

test("declining the confirmation buys nothing", async () => {
  const posts = [];
  const env = await boot(posts, { confirm: () => false });
  await env.mini.app.ACTIONS.addons(trigger({ "data-id": 7, "data-kind": "volume" }));
  await settle();
  await env.mini.app.ACTIONS["buy-addon"](trigger({ "data-id": 7, "data-pack": 1 }));
  assert.equal(posts.length, 0);
});

test("a pack that is not in the open list gives feedback instead of silence", async () => {
  const posts = [];
  const env = await boot(posts);
  await env.mini.app.ACTIONS.addons(trigger({ "data-id": 7, "data-kind": "volume" }));
  await settle();
  const before = env.toasts.length;
  await env.mini.app.ACTIONS["buy-addon"](trigger({ "data-id": 7, "data-pack": 99 }));
  assert.equal(posts.length, 0);
  assert.equal(env.toasts.length, before + 1, "the user is told why nothing happened");
});

test("a refused purchase shows the server's reason", async () => {
  const posts = [];
  const env = await boot(posts, { fail: true });
  await env.mini.app.ACTIONS.addons(trigger({ "data-id": 7, "data-kind": "volume" }));
  await settle();
  const before = env.toasts.length;
  await env.mini.app.ACTIONS["buy-addon"](trigger({ "data-id": 7, "data-pack": 1 }));
  await settle();
  assert.equal(posts.length, 1);
  assert.ok(env.toasts.length > before);
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
  console.log(`Mini App packs: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
