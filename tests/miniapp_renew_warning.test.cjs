"use strict";
// The renewal preview shows a carry-over loss warning above the numbers, escaped.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = vm.createContext({ URL, Intl });
for (const file of ["catalog_price.js", "miniapp/core.js", "miniapp/render.js"]) {
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app/web/static", file), "utf8"), context);
}
const { render } = context.PGMini;

const html = render.renewPreviewHtml(
  { plan_name: "Monthly", price: 1000, lines: ["حجم مانده: 1.5 GB (از بین می‌رود)"], warnings: ["⚠️ توجه <b>x</b>"], notice: "n" },
  1,
  2,
);
assert.ok(html.indexOf("⚠️ توجه") < html.indexOf("حجم مانده"), "the warning comes before the preview lines");
assert.ok(html.includes('role="alert"'));
assert.ok(html.includes("&lt;b&gt;x&lt;/b&gt;") && !html.includes("<b>x</b>"), "warning text is escaped");

const plain = render.renewPreviewHtml({ plan_name: "Monthly", price: 1000, lines: ["a"], warnings: [], notice: "" }, 1, 2);
assert.ok(!plain.includes("inline-error"), "no warning box without warnings");
console.log("Mini App renewal carry warning passed");
