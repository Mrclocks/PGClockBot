const assert = require("assert");
const api = require("../app/web/static/reseller-extra-packs.js");

assert.deepStrictEqual(api.parseCsvInts("1,5,10", api.DEFAULTS.gb), [1, 5, 10]);
assert.deepStrictEqual(api.parseCsvInts("", api.DEFAULTS.users), api.DEFAULTS.users.slice());
assert.deepStrictEqual(api.DEFAULTS.gb, [1, 5, 10, 50]);
console.log("reseller_extra_packs.test.cjs ok");
