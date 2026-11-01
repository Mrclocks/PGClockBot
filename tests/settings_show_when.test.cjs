"use strict";
const assert = require("node:assert/strict");
const { isShown, controllerName } = require("../app/web/static/settings-show-when.js");

assert.equal(controllerName("s_crypto_rate_mode:manual,api"), "s_crypto_rate_mode");
assert.equal(isShown("s_crypto_rate_mode:manual,api", "manual"), true);
assert.equal(isShown("s_crypto_rate_mode:manual,api", "api"), true);
assert.equal(isShown("s_crypto_rate_mode:manual,api", "off"), false);
assert.equal(isShown("s_crypto_rate_mode:api", "manual"), false);
assert.equal(isShown("s_crypto_rate_mode:api", ""), false);
// A malformed rule never hides a field.
assert.equal(isShown("", "off"), true);
assert.equal(isShown("nocolon", "off"), true);
console.log("settings show-when: ok");
