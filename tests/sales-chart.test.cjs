'use strict';
const assert = require('assert');
const chart = require('../app/web/static/sales-chart.js');

assert.strictEqual(chart.compactToman(4820000), '4.82 میلیون');
assert.strictEqual(chart.compactToman(250000), '250 هزار');
assert.strictEqual(chart.compactToman(1000000), '1 میلیون');
assert.strictEqual(chart.compactToman(900), '900');
assert.strictEqual(chart.compactToman(0), '0');
assert.strictEqual(chart.fullToman(27420000), '27,420,000');

assert.strictEqual(chart.compactToman(1500), '1.5 هزار');

// The scale covers the peak with headroom but leaves no more than about half of the plot empty.
for (const peak of [900, 1500, 5800000, 6100000, 8500000, 10000000, 27420000, 999999999]) {
  const scale = chart.niceScale(peak);
  assert.ok(scale.max >= peak * 1.1, `headroom above ${peak}`);
  assert.ok(peak / scale.max >= 0.55, `${peak} fills the plot (max ${scale.max})`);
  assert.strictEqual(scale.max, scale.step * 4);
}
assert.strictEqual(chart.niceScale(6100000).max, 8000000);
assert.ok(chart.niceScale(0).step > 0, 'empty data still has a scale');

// Counts (orders, new users) step by whole numbers and still cover the peak.
for (const peak of [0, 1, 3, 7, 11, 37, 240]) {
  const scale = chart.niceCountScale(peak);
  assert.ok(Number.isInteger(scale.step) && scale.step >= 1, `whole step for ${peak}`);
  assert.ok(scale.max >= peak, `covers ${peak}`);
  assert.strictEqual(scale.max, scale.step * 4);
}
assert.strictEqual(chart.niceCountScale(3).max, 4);

// Oldest day at the right edge, today at the left edge, evenly spaced.
assert.strictEqual(chart.dayX(0, 7, 600), 600);
assert.strictEqual(chart.dayX(6, 7, 600), 0);
assert.strictEqual(chart.dayX(3, 7, 600), 300);
assert.strictEqual(chart.dayX(0, 1, 600), 300);

// A monotone curve never goes above the highest or below the lowest control value.
const points = [[0, 100], [50, 20], [100, 20], [150, 90], [200, 10]];
const path = chart.monotonePath(points);
const numbers = path.match(/-?\d+(\.\d+)?/g).map(Number);
const ys = [];
for (let i = 1; i < numbers.length; i += 2) ys.push(numbers[i]);
assert.ok(Math.min(...ys) >= 10 - 1e-9 && Math.max(...ys) <= 100 + 1e-9, 'no overshoot');
assert.ok(path.startsWith('M0,100'));
assert.strictEqual(chart.monotonePath([]), '');
assert.strictEqual(chart.monotonePath([[5, 5]]), 'M5,5');
console.log('sales-chart ok');
