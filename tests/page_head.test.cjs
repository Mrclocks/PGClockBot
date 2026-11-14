"use strict";
const assert = require("node:assert/strict");
const {nextOpenState} = require("../app/web/static/page-head.js");

// The «⋯» button toggles the menu.
assert.equal(nextOpenState(false, "toggle"), true);
assert.equal(nextOpenState(true, "toggle"), false);
// Escape, a tap outside, choosing an action and a layout change (phone → desktop) always close it.
for (const event of ["escape", "outside", "item", "layout"]) {
  assert.equal(nextOpenState(true, event), false, event);
  assert.equal(nextOpenState(false, event), false, event);
}
// Anything else leaves it as it was.
assert.equal(nextOpenState(true, "scroll"), true);
assert.equal(nextOpenState(false, "scroll"), false);
console.log("Page header «⋯» menu: toggle, close events and no-op events passed");
