"use strict";
const assert = require("node:assert/strict");
const {nextView} = require("../app/web/static/modal-list.js");

const list = {name: "list", key: ""};
// A row opens its section; back and closing the modal return to the list.
assert.deepEqual(nextView(list, "open", "billing"), {name: "section", key: "billing"});
assert.deepEqual(nextView({name: "section", key: "billing"}, "back"), list);
assert.deepEqual(nextView({name: "section", key: "billing"}, "close"), list);
// Opening another row from a section switches straight to it.
assert.deepEqual(nextView({name: "section", key: "payment"}, "open", "cleanup"), {name: "section", key: "cleanup"});
// An open without a key, or an unknown action, keeps the view.
assert.deepEqual(nextView(list, "open", ""), list);
assert.deepEqual(nextView({name: "section", key: "x"}, "scroll"), {name: "section", key: "x"});
console.log("Settings modal list: open, back, close and no-op actions passed");
