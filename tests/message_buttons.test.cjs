// Buttons under a staff message (message-buttons.js): limits, the link and plan fields, filling from a saved set,
// and the Telegram-style preview (two buttons to a row, tags dropped from the text).
"use strict";

const assert = require("assert");
const { canAdd, needsUrl, needsPlan, presetButtons, previewRows, plainText } = require("../app/web/static/message-buttons.js");

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

assert.strictEqual(needsPlan("plan"), true);
assert.strictEqual(needsPlan("shop"), false);

assert.deepStrictEqual(previewRows([1, 2, 3]), [[1, 2], [3]]);
assert.deepStrictEqual(previewRows([1, 2, 3, 4]), [[1, 2], [3, 4]]);
assert.deepStrictEqual(previewRows([]), []);

assert.strictEqual(plainText("<b>سلام</b> &lt;دوست&gt;<br>خط دوم"), "سلام <دوست>\nخط دوم");
assert.strictEqual(plainText('<a href="https://x.example">لینک</a>'), "لینک");
assert.strictEqual(plainText(null), "");

console.log("message_buttons tests passed");
