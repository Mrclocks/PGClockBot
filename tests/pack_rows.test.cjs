const assert = require("assert");
const rows = require("../app/web/static/pack-rows.js");
const ds = require("../app/web/static/panel-ds.js");

// Pack price = unit price × amount; formatted rates are accepted, missing values show no price.
assert.strictEqual(rows.packPrice("6,000", 10), 60000);
assert.strictEqual(rows.packPrice(6000, 1), 6000);
assert.strictEqual(rows.packPrice("", 5), null);
assert.strictEqual(rows.packPrice(6000, 0), null);
assert.strictEqual(rows.packPrice("abc", 3), null);
assert.strictEqual(rows.faDigits(14), "۱۴");
assert.strictEqual(ds.faDigits("5 از 28"), "۵ از ۲۸");
console.log("pack_rows.test.cjs ok");
