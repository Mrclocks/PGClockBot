const assert = require("assert");
const ds = require("../app/web/static/panel-ds.js");

// Minimal stand-ins for the field, its «نامحدود» button and the wrapper; the module only
// reads and writes value, readOnly, aria-pressed and the is-unlimited class.
function control({ value, attrs = {} }) {
  const input = {
    value,
    readOnly: false,
    dataset: {},
    focused: false,
    closest: () => wrap,
    dispatchEvent() {},
    focus() { this.focused = true; },
  };
  const wrap = { classList: { toggle() {} } };
  const attributes = { "aria-pressed": "false", "data-ds-unlimited": "#field", "data-ds-empty-is-unlimited": "", ...attrs };
  const button = {
    closest: () => ({ querySelector: () => input }),
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
    hasAttribute: (name) => name in attributes,
    setAttribute: (name, v) => { attributes[name] = v; },
  };
  return { input, button, pressed: () => attributes["aria-pressed"] };
}

// Switching «نامحدود» off on a field that was empty gives it a number, so the field unlocks.
const empty = control({ value: "", attrs: { "data-ds-default": "50" } });
ds.syncUnlimited(empty.button);
assert.strictEqual(empty.pressed(), "true");
assert.strictEqual(empty.input.readOnly, true);
ds.toggleUnlimited(empty.button);
assert.strictEqual(empty.input.value, "50");
assert.strictEqual(empty.pressed(), "false");
assert.strictEqual(empty.input.readOnly, false);
assert.strictEqual(empty.input.focused, true);

// Without a configured default it still ends on a number rather than staying unlimited.
const bare = control({ value: "" });
ds.syncUnlimited(bare.button);
ds.toggleUnlimited(bare.button);
assert.strictEqual(bare.input.value, "1");
assert.strictEqual(bare.pressed(), "false");

// The number typed before going unlimited comes back.
const typed = control({ value: "20", attrs: { "data-ds-default": "50" } });
ds.syncUnlimited(typed.button);
ds.toggleUnlimited(typed.button);
assert.strictEqual(typed.input.value, "");
assert.strictEqual(typed.pressed(), "true");
ds.toggleUnlimited(typed.button);
assert.strictEqual(typed.input.value, "20");
console.log("ds_unlimited_toggle.test.cjs ok");
