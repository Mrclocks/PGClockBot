// Buttons under a staff message (message-buttons.js): limits, the link field and filling from a saved set.
"use strict";

const assert = require("assert");
const { canAdd, needsUrl, presetButtons } = require("../app/web/static/message-buttons.js");

assert.strictEqual(canAdd(0, 4), true);
assert.strictEqual(canAdd(3, 4), true);
assert.strictEqual(canAdd(4, 4), false);

assert.strictEqual(needsUrl("link"), true);
assert.strictEqual(needsUrl("shop"), false);
assert.strictEqual(needsUrl(""), false);

const presets = [
  { id: 3, name: "فروش", buttons: [{ target: "shop" }, { target: "wallet" }, { target: "support" }, { target: "club" }, { target: "services" }] },
];
assert.deepStrictEqual(presetButtons(presets, "3", 4).map((b) => b.target), ["shop", "wallet", "support", "club"]);
assert.deepStrictEqual(presetButtons(presets, "9", 4), []);
assert.deepStrictEqual(presetButtons(null, "3", 4), []);

console.log("message_buttons tests passed");
