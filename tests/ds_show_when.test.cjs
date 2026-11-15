const assert = require("assert");
const ds = require("../app/web/static/panel-ds.js");

// A field shown for some values of another field; the selector itself may contain no colon.
assert.strictEqual(ds.showWhenMatches("#basis:user,user_traffic", "user"), true);
assert.strictEqual(ds.showWhenMatches("#basis:user,user_traffic", "traffic"), false);
// An unchecked switch reads as "0", a checked one as its value.
assert.strictEqual(ds.showWhenMatches("#cb-enabled:1", "1"), true);
assert.strictEqual(ds.showWhenMatches("#cb-enabled:1", "0"), false);
// A malformed rule never hides anything.
assert.strictEqual(ds.showWhenMatches("", "x"), true);
assert.strictEqual(ds.showWhenMatches("no-colon", "x"), true);
console.log("ds_show_when.test.cjs ok");
