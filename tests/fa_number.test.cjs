"use strict";
const assert = require("node:assert/strict");
const {faDigits, faNumber, formatInt} = require("../app/web/static/fa-number.js");

assert.equal(faDigits(2026), "۲۰۲۶");
assert.equal(faNumber("1,234.5"), "۱٬۲۳۴٫۵");
// A point that is not between two digits stays a point.
assert.equal(faNumber("v1. 2"), "v۱. ۲");
assert.equal(formatInt(390000), "۳۹۰٬۰۰۰");
assert.equal(formatInt(-1500.6), "-۱٬۵۰۱");
assert.equal(formatInt("not a number"), "۰");
console.log("Persian figures: digits, thousands, decimal mark and bad input passed");
