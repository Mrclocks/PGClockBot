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
// A new pack line takes the editor's full colour list, even when the editor has no line to copy from.
function fakeEditor({ list = null, sample = null } = {}) {
  return {
    querySelector(sel) {
      if (sel === "template[data-pack-style-options]") return list === null ? null : { innerHTML: list };
      if (sel === "select.extra-pack-style") return sample === null ? null : { innerHTML: sample };
      return null;
    },
  };
}
const ALL = '<option value="inherit">پیش‌فرض</option><option value="primary">آبی</option>';
assert.strictEqual(rows.styleOptionsHtml(fakeEditor({ list: ALL })), ALL);
assert.strictEqual(rows.styleOptionsHtml(fakeEditor({ list: ALL, sample: "<option>x</option>" })), ALL);
assert.strictEqual(rows.styleOptionsHtml(fakeEditor({ list: "  ", sample: "<option>x</option>" })), "<option>x</option>");
assert.strictEqual(rows.styleOptionsHtml(fakeEditor()), '<option value="inherit">پیش‌فرض</option>');
assert.strictEqual(rows.styleOptionsHtml(null), '<option value="inherit">پیش‌فرض</option>');
console.log("pack_rows.test.cjs ok");
