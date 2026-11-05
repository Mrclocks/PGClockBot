const assert = require("assert");
const api = require("../app/web/static/user-plan-addon-packs.js");

assert.strictEqual(typeof api.buildRow, "function");
assert.strictEqual(typeof api.bind, "function");
assert.strictEqual(typeof api.mount, "function");
assert.strictEqual(typeof api.setEditorVisible, "function");
assert.strictEqual(typeof api.seedDefaultsIfEmpty, "function");
assert.deepStrictEqual(api.DEFAULTS.volume, [1, 5, 10, 50]);
assert.deepStrictEqual(api.DEFAULTS.duration, [1, 3, 6, 12]);
assert.deepStrictEqual(api.parseCsvInts("1,3,6", api.DEFAULTS.duration), [1, 3, 6]);

console.log("user_plan_addon_packs.test.cjs ok");
