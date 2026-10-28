"use strict";
const assert = require("node:assert/strict");
const {sortOptions, parseSortValue} = require("../app/web/static/table-list.js");

const options = sortOptions([
  null,
  {listLabel: "نام", listDir: "ascending", listOrder: "4", sortType: "text"},
  {listLabel: "", sortType: "num"},
  {listLabel: "زودترین انقضا", listDir: "ascending", listOrder: "2", sortType: "num"},
  {listLabel: "تلگرام", listDir: "ascending", listOrder: "3", sortType: null},
  {listLabel: "جدیدترین", listDir: "descending", listOrder: "1", sortType: "num"},
]);
assert.deepEqual(options, [
  {value: "5:descending", label: "جدیدترین"},
  {value: "3:ascending", label: "زودترین انقضا"},
  {value: "1:ascending", label: "نام"},
], "only marked, sortable columns, one direction each, in the marked order");

assert.deepEqual(
  sortOptions([
    {listLabel: "الف", sortType: "text"},
    {listLabel: "ب", listDir: "sideways", sortType: "text"},
  ]).map((o) => o.value),
  ["0:ascending", "1:ascending"],
  "an unknown or missing direction falls back to ascending; unordered columns keep header order",
);

assert.deepEqual(
  sortOptions([{listLabel: "جدیدترین", listDir: "descending", sortType: "num"}, {label: "نام", sortType: "text"}]),
  [],
  "a single choice is no menu",
);

assert.deepEqual(parseSortValue("3:descending"), {index: 3, direction: "descending"});
for (const bad of ["", "x:ascending", "-1:ascending", "2:sideways", "1.5:ascending", "1:ascending:2"]) {
  assert.equal(parseSortValue(bad), null, `rejects ${JSON.stringify(bad)}`);
}
console.log("table_list ok");
