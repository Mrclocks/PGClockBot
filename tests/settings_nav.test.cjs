"use strict";
const assert = require("node:assert/strict");
const { firstPageTarget, DESKTOP_QUERY } = require("../app/web/static/settings-nav.js");

assert.equal(DESKTOP_QUERY, "(min-width: 1200px)");
assert.equal(firstPageTarget("/settings?tab=identity", true, ""), "/settings?tab=identity", "a desktop opens the first page");
assert.equal(firstPageTarget("/settings?tab=identity", false, ""), null, "a phone or tablet keeps the list");
assert.equal(firstPageTarget("/settings?tab=general", true, "?tab=system&ok=saved"), null, "a saved message stays on the list");
assert.equal(firstPageTarget("/settings?tab=general", true, "?tab=system&err=x"), null, "an error stays on the list");
for (const bad of ["", null, "https://evil.example/x", "//evil.example", "javascript:alert(1)", "settings"]) {
  assert.equal(firstPageTarget(bad, true, ""), null, `target ${JSON.stringify(bad)} is refused`);
}
console.log("settings_nav tests passed");
