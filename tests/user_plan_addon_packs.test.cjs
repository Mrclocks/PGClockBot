const assert = require("assert");
const api = require("../app/web/static/user-plan-addon-packs.js");

assert.strictEqual(typeof api.buildRow, "function");
assert.strictEqual(typeof api.bind, "function");
assert.strictEqual(typeof api.mount, "function");

console.log("user_plan_addon_packs.test.cjs ok");
