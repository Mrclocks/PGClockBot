const assert = require("node:assert/strict");
const path = require("node:path");
const { isTextEntry, isKeyboardUp, viewportTracker, KEYBOARD_MIN_PX } =
  require(path.join(__dirname, "..", "app", "web", "static", "panel-keyboard.js"));

// Only fields that raise a text keyboard count.
assert.equal(isTextEntry({tagName: "INPUT", type: "search"}), true);
assert.equal(isTextEntry({tagName: "INPUT"}), true);
assert.equal(isTextEntry({tagName: "TEXTAREA"}), true);
assert.equal(isTextEntry({tagName: "DIV", isContentEditable: true}), true);
assert.equal(isTextEntry({tagName: "INPUT", type: "checkbox"}), false);
assert.equal(isTextEntry({tagName: "INPUT", type: "Range"}), false);
assert.equal(isTextEntry({tagName: "SELECT"}), false);
assert.equal(isTextEntry({tagName: "BUTTON"}), false);
assert.equal(isTextEntry(null), false);

// A focused field with the screen unchanged (focused on load, hardware keyboard) keeps the bar.
assert.equal(isKeyboardUp(800, 800, true), false);
assert.equal(isKeyboardUp(800, 800 - KEYBOARD_MIN_PX + 1, true), false);
// The keyboard took the bottom of the screen.
assert.equal(isKeyboardUp(800, 480, true), true);
// The screen shrank for another reason with nothing focused.
assert.equal(isKeyboardUp(800, 480, false), false);

// The tracker remembers the tallest height at a width and starts over when the width changes.
const fullAt = viewportTracker();
assert.equal(fullAt(390, 700), 700);
assert.equal(fullAt(390, 400), 700);
assert.equal(fullAt(390, 760), 760);
assert.equal(fullAt(844, 300), 300);
assert.equal(fullAt(844, 360), 360);
console.log("panel_keyboard tests passed");
