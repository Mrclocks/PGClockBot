"use strict";
const assert = require("node:assert/strict");
const { messageTarget } = require("../app/web/static/user-message.js");

assert.deepEqual(messageTarget("42", "Aboli"), { action: "/users/42/message", label: "Aboli" });
assert.equal(messageTarget("42", "").label, "#42", "a person without a name is shown by id");
assert.equal(messageTarget("42", "<b>x</b>").label, "<b>x</b>", "the name stays plain text for textContent");
for (const bad of ["", "0", "-1", "1.5", "12/../x", "1 ", "99999999999999999999", null]) {
  assert.equal(messageTarget(bad, "x"), null, `id ${JSON.stringify(bad)} is refused`);
}
console.log("user_message tests passed");
