"use strict";
const assert = require("node:assert/strict");
const { sectionTitle, CARD_OWN_MODALS } = require("../app/web/static/user-card-actions.js");

assert.equal(sectionTitle("پرداخت ۱۲"), "اقدام‌های پرداخت ۱۲");
assert.equal(sectionTitle("  "), "اقدام‌های این ردیف", "a row without a label gets the generic title");
assert.equal(sectionTitle(null), "اقدام‌های این ردیف");
assert.equal(sectionTitle("<b>x</b>"), "اقدام‌های <b>x</b>", "the label is plain text for textContent");
for (const own of ["modal-user-card", "modal-user-message", "modal-user-edit", "modal-reseller-edit"]) {
  assert.ok(CARD_OWN_MODALS.includes(own), `${own} is never copied: the card already has it`);
}
console.log("user_card_actions tests passed");
