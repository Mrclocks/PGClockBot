"use strict";
const assert = require("node:assert/strict");
const { previewRows, attachableId, errorText } = require("../app/web/static/service-import-assign.js");
const { faNumber } = require("../app/web/static/fa-number.js");

const preview = {
  ok: true, panel_user_id: 11, panel_username: "plat-one", status: "فعال",
  used: "1 گیگ", limit: "نامحدود", expire: "—", already_yours: false,
};

assert.deepEqual(previewRows(preview).map((row) => row[0]), ["کاربر پنل", "وضعیت", "مصرف", "حجم", "انقضا"]);
assert.equal(previewRows(preview)[0][1], "plat-one");
assert.deepEqual(previewRows({ ok: true }).map((row) => row[1]), ["—", "—", "—", "—", "—"]);
const shown = previewRows({ ...preview, panel_username: "user10", used: "3 گیگ", expire: "2027/01/20 · 65 روز" }, faNumber);
assert.equal(shown[0][1], "user10", "the panel username keeps its own digits");
assert.equal(shown[2][1], "۳ گیگ");
assert.equal(shown[4][1], "۲۰۲۷/۰۱/۲۰ · ۶۵ روز");
assert.equal(previewRows({ panel_username: { toString: () => "<b>x</b>" } })[0][1], "—", "only strings are shown");

assert.equal(attachableId(preview), "11");
assert.equal(attachableId({ ...preview, already_yours: true }), "", "a service already in the list is not attached again");
for (const id of [0, -1, 1.5, "11", null, undefined, true, Number.NaN]) {
  assert.equal(attachableId({ ...preview, panel_user_id: id }), "", String(id));
}
assert.equal(attachableId({ ...preview, ok: false }), "");
assert.equal(attachableId(null), "");

assert.equal(errorText({ ok: false, error: "این سرویس به مشتری دیگری وصل است." }), "این سرویس به مشتری دیگری وصل است.");
assert.equal(errorText(null), "بررسی انجام نشد. دوباره تلاش کنید.");
assert.equal(errorText({ error: 42 }), "بررسی انجام نشد. دوباره تلاش کنید.");
assert.equal(errorText({ error: "   " }), "بررسی انجام نشد. دوباره تلاش کنید.");

console.log("service_import_assign.test.cjs ok");
