'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { rangeError } = require('../app/web/static/finance-reports.js');
assert.equal(rangeError('2026-10-17T10:15', '2026-10-17T12:45'), '');
assert.equal(rangeError('2026-10-17T23:59', '2026-10-18T00:00'), '');
assert.ok(rangeError('', '2026-10-17T12:45'));
assert.ok(rangeError('2026-10-17T10:15', ''));
assert.ok(rangeError('2026-10-17T12:45', '2026-10-17T10:15'));
assert.ok(rangeError('2026-10-17T10:15', '2026-10-17T10:15'));

const handlers = {}, windowHandlers = {}, attributes = {};
const start = {value: '2026-10-17T10:15', focused: false, focus() { this.focused = true; }};
const end = {
  value: '2026-10-17T09:00', message: '', reported: false,
  setCustomValidity(message) { this.message = message; },
  reportValidity() { this.reported = true; },
};
const status = {textContent: ''}, button = {disabled: false};
const form = {
  hidden: true,
  elements: {start, end},
  querySelector(selector) { return selector === '[data-finance-status]' ? status : button; },
  addEventListener(name, handler) { handlers[name] = handler; },
  setAttribute(name, value) { attributes[name] = value; },
  removeAttribute(name) { delete attributes[name]; },
};
const toggleAttributes = {}, toggleHandlers = {};
const customToggle = {
  addEventListener(name, handler) { toggleHandlers[name] = handler; },
  setAttribute(name, value) { toggleAttributes[name] = value; },
};
const window = {
  document: {querySelector(selector) { return selector === '[data-finance-range]' ? form : customToggle; }},
  addEventListener(name, handler) { windowHandlers[name] = handler; },
};
vm.runInNewContext(fs.readFileSync(require.resolve('../app/web/static/finance-reports.js'), 'utf8'), {window});
assert.equal(form.hidden, true, 'preset reports keep the date/time form hidden');
toggleHandlers.click();
assert.equal(form.hidden, false, 'custom range reveals the complete form');
assert.equal(toggleAttributes['aria-expanded'], 'true');
assert.equal(start.focused, true, 'opening the form focuses its first input');
toggleHandlers.click();
assert.equal(form.hidden, true);
assert.equal(toggleAttributes['aria-expanded'], 'false');
toggleHandlers.click();
handlers.input();
assert.ok(end.message, 'invalid end is marked before submission');
let prevented = false;
handlers.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(end.reported, true);
assert.equal(button.disabled, false, 'invalid filters can be corrected immediately');
assert.equal(status.textContent, end.message);
end.value = '2026-10-17T12:45';
handlers.input();
assert.equal(end.message, '', 'correcting the range clears validity');
handlers.submit({preventDefault() { throw new Error('valid range was blocked'); }});
assert.equal(button.disabled, true);
assert.equal(attributes['aria-busy'], 'true');
assert.ok(status.textContent);
windowHandlers.pageshow();
assert.equal(button.disabled, false, 'back navigation restores the submit button');
assert.equal(attributes['aria-busy'], undefined);
assert.equal(status.textContent, '');
console.log('finance report range feedback passed');
