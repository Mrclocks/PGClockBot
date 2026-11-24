// Group-message composer (broadcast-compose.js): the audience query, the send button and the confirm question.
"use strict";

const assert = require("assert");
const { audienceQuery, sendLabel, confirmText, daysHint } = require("../app/web/static/broadcast-compose.js");

assert.strictEqual(audienceQuery({ segment: "expiring", days: "5", plan_id: "", min_wallet: "" }), "segment=expiring&days=5");
assert.strictEqual(audienceQuery({ segment: "all", notice: true }), "segment=all&notice=1");
assert.strictEqual(audienceQuery({ segment: "buyers", plan_id: "7", min_wallet: " 1000 " }), "segment=buyers&plan_id=7&min_wallet=1000");
// Typed text never leaves the query unencoded.
assert.strictEqual(audienceQuery({ segment: "a&b=c" }), "segment=a%26b%3Dc");

assert.strictEqual(sendLabel(null), "ارسال پیام");
assert.strictEqual(sendLabel(0), "مخاطبی ندارد");
assert.strictEqual(sendLabel(12), "ارسال برای 12 نفر");

assert.strictEqual(confirmText("نزدیک انقضا · 3 روز", 4), "پیام برای «نزدیک انقضا · 3 روز» (4 نفر) در صف ارسال قرار بگیرد؟ پیامِ فرستاده‌شده پس گرفته نمی‌شود.");
assert.strictEqual(confirmText("", null), "پیام برای گروه انتخاب‌شده در صف ارسال قرار بگیرد؟ پیامِ فرستاده‌شده پس گرفته نمی‌شود.");

assert.strictEqual(daysHint(7), "خالی بماند: 7 روز");
assert.strictEqual(daysHint(0), "");

console.log("broadcast_compose tests passed");
