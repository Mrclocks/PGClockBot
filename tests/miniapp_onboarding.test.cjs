"use strict";
// Mini App onboarding: the referral code, required channels and entry terms are finished in the
// Mini App; each step is escaped, sent once per tap, and a "ready" answer never loops.
const assert = require("node:assert/strict");
const { loadMiniApp, trigger, settle } = require("./miniapp_env.cjs");

const HOSTILE = "<img src=x onerror=alert(1)>";
const ME = {
  persona: "user", commerce_allowed: true, currency: "تومان", panel_base: "",
  nav: [{ id: "home", label: "خانه", icon: "home" }, { id: "account", label: "حساب", icon: "account" }],
  user: { id: 1, name: "a" },
  customer: { wallet: 0, wallet_pay_enabled: false, qr_enabled: false, commerce_allowed: true, home: { service_count: 0, attention: [], recent: [] } },
};
const FORCE_JOIN = {
  step: "force_join", text: "عضو شوید " + HOSTILE, check_label: "عضو شدم",
  channels: [
    { label: HOSTILE, url: "https://t.me/news", status: "missing" },
    { label: "خصوصی", url: "", status: "unverified" },
    { label: "بد", url: "javascript:alert(1)", status: "missing" },
  ],
};
const TERMS = { step: "terms", text: "قوانین <script>alert(1)</script>", button: "می‌پذیرم" };
const REFERRAL = { step: "referral", text: "کد معرف لازم است", invalid: false };
const gate = (code) => ({ status: 403, body: { detail: "مرحله" }, headers: { "X-Mini-Error": code } });

/** routes: [regexp on "METHOD url", handler(body, url, count)] tried in order, then defaults. */
function router(rules) {
  const counts = new Map();
  return async (method, url, body) => {
    const key = method + " " + url;
    counts.set(key, (counts.get(key) || 0) + 1);
    for (const [pattern, handler] of rules) {
      if (pattern.test(key)) return handler(body, url, counts.get(key));
    }
    if (url.startsWith("/api/mini/activity")) return { status: 200, body: { items: [] } };
    return { status: 404, body: { detail: "پیدا نشد" } };
  };
}

async function boot(rules, options) {
  const env = loadMiniApp({ routes: router(rules), ...(options || {}) });
  env.mini.app.start();
  await settle();
  return env;
}

function form(kind, fields) {
  const button = { disabled: false, isConnected: true };
  const elements = {};
  for (const [k, v] of Object.entries(fields)) elements[k] = { value: v };
  return { elements, getAttribute: (n) => ({ "data-form": kind }[n] ?? null), querySelector: () => button };
}

const html = (env) => env.element("root").innerHTML;
const count = (env, re) => env.calls.filter((c) => re.test((c.method || "GET") + " " + c.url)).length;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("a gate on launch shows that step, escaped, without the tab bar", async () => {
  const env = await boot([
    [/GET \/api\/mini\/me$/, () => gate("force_join")],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: FORCE_JOIN })],
  ]);
  const page = html(env);
  assert.ok(page.includes('data-action="onboarding-join-check"'));
  assert.ok(!page.includes("<img src=x"), "channel labels and text are escaped");
  assert.ok(page.includes("&lt;img src=x"));
  assert.ok(page.includes("لینک عضویت تنظیم نشده"), "a channel without a link cannot be opened");
  assert.equal(env.element("nav").hidden, true);
  assert.equal(env.mini.app.state.me, null);
});

test("a channel opens only through its Telegram link", async () => {
  const env = await boot([
    [/GET \/api\/mini\/me$/, () => gate("force_join")],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: FORCE_JOIN })],
  ]);
  env.mini.app.ACTIONS["onboarding-channel"](trigger({ "data-index": 0 }));
  env.mini.app.ACTIONS["onboarding-channel"](trigger({ "data-index": 2 }));
  env.mini.app.ACTIONS["onboarding-channel"](trigger({ "data-index": 9 }));
  const opened = env.calls.filter((c) => c.method === "OPEN").map((c) => c.url);
  assert.deepEqual(opened, ["https://t.me/news"]);
});

test("joined: one request per tap, then the app opens", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const env = await boot([
    [/GET \/api\/mini\/me$/, (_b, _u, n) => (n === 1 ? gate("force_join") : { status: 200, body: ME })],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: FORCE_JOIN })],
    [/POST \/api\/mini\/onboarding\/join-check$/, async () => { await pending; return { status: 200, body: { step: "ready" } }; }],
  ]);
  const button = trigger({});
  const first = env.mini.app.ACTIONS["onboarding-join-check"](button);
  env.mini.app.ACTIONS["onboarding-join-check"](button);
  release();
  await first;
  await settle();
  assert.equal(count(env, /POST \/api\/mini\/onboarding\/join-check$/), 1);
  assert.equal(env.mini.app.state.me.user.id, 1, "the app booted after the last step");
  assert.equal(env.mini.app.state.onboarding, null);
});

test("a step that is still not joined stays on screen with the server's reason", async () => {
  const env = await boot([
    [/GET \/api\/mini\/me$/, () => gate("force_join")],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: FORCE_JOIN })],
    [/POST \/api\/mini\/onboarding\/join-check$/, () => ({ status: 429, body: { detail: "چند ثانیه صبر کنید" } })],
  ]);
  await env.mini.app.ACTIONS["onboarding-join-check"](trigger({}));
  await settle();
  assert.ok(html(env).includes("چند ثانیه صبر کنید"));
  assert.ok(html(env).includes('data-action="onboarding-join-check"'));
});

test("'ready' from onboarding but a gate from /me shows an error instead of looping", async () => {
  const env = await boot([
    [/GET \/api\/mini\/me$/, () => gate("terms")],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: { step: "ready" } })],
  ]);
  assert.equal(count(env, /GET \/api\/mini\/onboarding$/), 1);
  assert.equal(count(env, /GET \/api\/mini\/me$/), 2);
  assert.ok(html(env).includes('role="alert"'));
});

test("the referral form keeps the typed code, escaped, when the code is wrong", async () => {
  const env = await boot([
    [/GET \/api\/mini\/me$/, (_b, _u, n) => (n === 1 ? gate("referral_required") : { status: 200, body: ME })],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: REFERRAL })],
    [/POST \/api\/mini\/onboarding\/referral$/, (body) => (body.code === "GOOD1"
      ? { status: 200, body: TERMS }
      : { status: 400, body: { detail: "کد معرف معتبر نیست" } })],
  ]);
  await env.mini.app.submitForm(form("onboarding-referral", { code: "   " }));
  assert.equal(count(env, /POST/), 0, "an empty code is never sent");
  await env.mini.app.submitForm(form("onboarding-referral", { code: '"><b>x' }));
  await settle();
  assert.ok(html(env).includes("کد معرف معتبر نیست"));
  assert.ok(html(env).includes('value="&quot;&gt;&lt;b&gt;x"'));
  await env.mini.app.submitForm(form("onboarding-referral", { code: " GOOD1 " }));
  await settle();
  const sent = env.calls.filter((c) => c.method === "POST").map((c) => c.body.code);
  assert.deepEqual(sent, ['"><b>x', "GOOD1"]);
  assert.ok(html(env).includes('data-action="onboarding-terms"'), "the next step follows the code");
  assert.ok(!html(env).includes("<script>"), "terms text is escaped");
});

test("leaving a channel after launch brings the step back", async () => {
  const env = await boot([
    [/GET \/api\/mini\/me$/, () => ({ status: 200, body: ME })],
    [/GET \/api\/mini\/activity/, () => gate("force_join")],
    [/GET \/api\/mini\/onboarding$/, () => ({ status: 200, body: FORCE_JOIN })],
  ]);
  assert.ok(env.mini.app.state.me);
  env.mini.app.go("account");
  await settle();
  assert.equal(env.mini.app.state.me, null);
  assert.ok(html(env).includes('data-action="onboarding-join-check"'));
});

test("someone the bot cannot message yet is asked once per device and bot", async () => {
  const asked = [];
  const storage = new Map();
  for (let i = 0; i < 2; i++) {
    const env = loadMiniApp({ routes: router([[/GET \/api\/mini\/me$/, () => ({ status: 200, body: ME })]]) });
    const tg = env.context.Telegram.WebApp;
    tg.initDataUnsafe = { user: { id: 7, allows_write_to_pm: false } };
    tg.isVersionAtLeast = () => true;
    tg.requestWriteAccess = () => asked.push(i);
    env.context.localStorage = { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) };
    env.mini.app.start();
    await settle();
  }
  assert.deepEqual(asked, [0]);
  const env = loadMiniApp({ routes: router([[/GET \/api\/mini\/me$/, () => ({ status: 200, body: ME })]]) });
  env.context.Telegram.WebApp.initDataUnsafe = { user: { id: 8, allows_write_to_pm: true } };
  env.context.Telegram.WebApp.requestWriteAccess = () => asked.push("again");
  env.mini.app.start();
  await settle();
  assert.deepEqual(asked, [0], "someone who already allowed messages is not asked");
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
  console.log(`Mini App onboarding: ${tests.length} checks passed`);
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
