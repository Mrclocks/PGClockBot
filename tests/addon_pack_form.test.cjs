"use strict";
const assert = require("node:assert/strict");
const { rulesFor, rowLimits, MAX_ROWS } = require("../app/web/static/addon-pack-form.js");

assert.equal(rulesFor("volume").unit, "گیگ");
assert.equal(rulesFor("duration").step, "1");
assert.equal(rulesFor("duration").min, "1");
assert.equal(rulesFor("unknown").unit, "گیگ");

assert.deepEqual(rowLimits(1, false), { canAdd: true, canRemove: false });
assert.deepEqual(rowLimits(3, false), { canAdd: true, canRemove: true });
assert.deepEqual(rowLimits(MAX_ROWS, false), { canAdd: false, canRemove: true });
assert.deepEqual(rowLimits(1, true), { canAdd: false, canRemove: false });
assert.equal(MAX_ROWS, 10);
console.log("addon_pack_form tests passed");
