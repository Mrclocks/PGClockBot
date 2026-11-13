const assert = require("assert");
const ds = require("../app/web/static/panel-ds.js");

const half = { shown: true, fullRow: false };
const full = { shown: true, fullRow: true };
const hidden = { shown: false, fullRow: false };

// An even run of fields pairs up; nothing spans.
assert.deepStrictEqual(ds.unpairedCells([half, half, half, half]), []);
// The last field of an odd run spans the row instead of sitting beside an empty half.
assert.deepStrictEqual(ds.unpairedCells([half, half, half]), [2]);
assert.deepStrictEqual(ds.unpairedCells([half]), [0]);
// A full-width row (switch, long field) closes the run before it and starts a new one.
assert.deepStrictEqual(ds.unpairedCells([half, full, half, half]), [0]);
assert.deepStrictEqual(ds.unpairedCells([full, half, full, half]), [1, 3]);
// Hidden fields (show-when alternatives, hidden inputs) take no place in the count.
assert.deepStrictEqual(ds.unpairedCells([half, hidden, half]), []);
assert.deepStrictEqual(ds.unpairedCells([half, hidden, hidden, half, half]), [4]);
assert.deepStrictEqual(ds.unpairedCells([]), []);
console.log("ds_pair_grid.test.cjs ok");
