"use strict";
const assert = require("node:assert/strict");
const {sortOptions, parseSortValue} = require("../app/web/static/table-list.js");

const options = sortOptions([
  null,
  {label: "نام", sortType: "text"},
  {label: "", sortType: "num"},
  {label: "انقضا", sortType: "num"},
  {label: "عملیات", sortType: null},
]);
assert.deepEqual(options.map((o) => o.value), ["1:ascending", "1:descending", "3:ascending", "3:descending"],
  "each sortable, titled column, both ways, keeping its header index");
assert.equal(options[2].label, "انقضا ↑");

assert.deepEqual(parseSortValue("3:descending"), {index: 3, direction: "descending"});
for (const bad of ["", "x:ascending", "-1:ascending", "2:sideways", "1.5:ascending", "1:ascending:2"]) {
  assert.equal(parseSortValue(bad), null, `rejects ${JSON.stringify(bad)}`);
}
console.log("table_list ok");
