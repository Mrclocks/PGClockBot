"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {groupMoneyDigits, formatMoneyInput} = require("../app/web/static/money-inputs.js");

for (const [raw, expected] of [
  ["", ""], ["0", "0"], ["999", "999"], ["1000", "1,000"],
  ["1234567890", "1,234,567,890"], ["-1250000", "-1,250,000"],
  ["9223372036854775807", "9,223,372,036,854,775,807"],
  ["1234.5", "1,234.5"], ["<img>", "<img>"], ["12abc", "12abc"],
]) assert.equal(groupMoneyDigits(raw), expected);

const normalize = (value) => value.replace(/[۰-۹٠-٩]/g, (digit) => {
  const code = digit.charCodeAt(0);
  return String(code >= 0x06f0 ? code - 0x06f0 : code - 0x0660);
}).replace(/[,٬\s]/g, "");
const field = (value, start, end = start) => ({
  value, selectionStart: start, selectionEnd: end,
  setSelectionRange(first, last) {this.selectionStart = first; this.selectionEnd = last;},
});
const typed = field("1250000", 7);
formatMoneyInput(typed, normalize);
assert.equal(typed.value, "1,250,000");
assert.equal(typed.selectionStart, 9);
assert.equal(normalize(typed.value), "1250000");
const edited = field("12,950,000", 4);
formatMoneyInput(edited, normalize);
assert.equal(edited.selectionStart, 4);
const inserted = field("1,2900,000", 4);
formatMoneyInput(inserted, normalize);
assert.equal(inserted.value, "12,900,000");
assert.equal(inserted.selectionStart, 4);
const deleted = field("1,50,000", 3);
formatMoneyInput(deleted, normalize);
assert.equal(deleted.value, "150,000");
assert.equal(deleted.selectionStart, 2);
const pasted = field("۱۲۵۰۰۰۰", 7);
formatMoneyInput(pasted, normalize);
assert.equal(pasted.value, "1,250,000");
assert.equal(pasted.selectionStart, 9);
const arabic = field("١٢٥٬٠٠٠", 7);
formatMoneyInput(arabic, normalize);
assert.equal(arabic.value, "125,000");
const selection = field("1234567", 1, 4);
formatMoneyInput(selection, normalize);
assert.equal(selection.selectionStart, 1);
assert.equal(selection.selectionEnd, 5);
const empty = field("", 0);
formatMoneyInput(empty, normalize);
assert.equal(empty.value, "");

const source = fs.readFileSync(path.join(__dirname, "../app/web/static/miniapp.js"), "utf8");
const context = vm.createContext({
  window: {addEventListener() {}}, location: {hash: ""},
  document: {getElementById: () => ({}), querySelectorAll: () => []},
});
vm.runInContext(source.replace(/  load\(\);\s*\}\)\(\);\s*$/, `
  globalThis.testUI = {money, plansHtml, activityHtml, setCurrency: (value) => {currency = value;}};
})();`), context);
const ui = context.testUI;
assert.equal(ui.money(1250000), "۱,۲۵۰,۰۰۰ تومان");
assert.equal(ui.money(0), "۰ تومان");
assert.ok(ui.plansHtml([{id: 1, price: 1250000, name: "<img>", days: 30}]).includes("۱,۲۵۰,۰۰۰"));
assert.ok(ui.plansHtml([{id: 1, price: 0, name: "<img>"}]).includes("&lt;img&gt;"));
assert.ok(ui.activityHtml([{amount: -1250000}]).includes("۱,۲۵۰,۰۰۰"));
ui.setCurrency("<img>");
assert.ok(ui.plansHtml([{id: 1, price: 1250000}]).includes("&lt;img&gt;"));
assert.ok(!ui.plansHtml([{id: 1, price: 1250000}]).includes("<img>"));
console.log("Web monetary inputs and Mini App price formatting passed");
