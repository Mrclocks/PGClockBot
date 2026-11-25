// Group-message composer (broadcast-compose.js): the audience query, the send button and the confirm question.
"use strict";

const assert = require("assert");
const { audienceQuery, sendLabel, confirmText, daysHint, testStatus, mediaKind, textRules } = require("../app/web/static/broadcast-compose.js");

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

// The test send shows the server's answer, and a safe line when there is none.
assert.deepStrictEqual(testStatus(true, { message: "فرستاده شد" }), { ok: true, text: "فرستاده شد" });
assert.deepStrictEqual(testStatus(false, { error: "رد شد" }), { ok: false, text: "رد شد" });
assert.deepStrictEqual(testStatus(true, null), { ok: false, text: "ارسال آزمایشی انجام نشد؛ اتصال را بررسی کنید." });
assert.deepStrictEqual(testStatus(false, { message: "not an error" }).ok, false);

// A chosen file is recognised by type, then by extension; anything else is not media.
assert.strictEqual(mediaKind({ type: "image/png", name: "a.png" }), "photo");
assert.strictEqual(mediaKind({ type: "video/mp4", name: "clip" }), "video");
assert.strictEqual(mediaKind({ type: "", name: "PHOTO.JPG" }), "photo");
assert.strictEqual(mediaKind({ type: "application/pdf", name: "x.pdf" }), null);
assert.strictEqual(mediaKind(null), null);

// With media the text is an optional caption limited to Telegram's caption length.
assert.deepStrictEqual(textRules(true, "1024"), { required: false, maxLength: 1024 });
assert.deepStrictEqual(textRules(false, "1024"), { required: true, maxLength: 4000 });

console.log("broadcast_compose tests passed");
