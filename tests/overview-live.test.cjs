'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');
const live = require(path.join(__dirname, '..', 'app', 'web', 'static', 'overview-live.js'));

assert.equal(live.fmtPct(41.6), '42٪');
assert.equal(live.fmtPct(140), '100٪');
assert.equal(live.fmtPct(null), '—');
assert.equal(live.fmtPct(NaN), '—');
assert.equal(live.clampPct(-5), 0);

assert.equal(live.memHint({ memory_ratio_text: '2 / 4 GB' }), '2 / 4 GB');
assert.equal(live.memHint({ memory_used_text: '2', memory_total_text: '4 GB' }), '2 / 4 GB');
assert.equal(live.memHint({}), null);

assert.equal(live.partsText({ num: '1.5', unit: 'GB' }), '1.5 GB');
assert.equal(live.partsText({ num: 0 }), '0');
assert.equal(live.partsText(null), '—');

let buf = [];
for (let i = 0; i < live.SAMPLES + 5; i += 1) buf = live.pushSample(buf, i, live.SAMPLES);
assert.equal(buf.length, live.SAMPLES, 'the ring buffer stays bounded');
assert.equal(buf[buf.length - 1], live.SAMPLES + 4);
assert.deepEqual(live.pushSample([], 'x', 3), [0], 'non-numbers become zero');

assert.equal(live.sparkPoints([], 10, 300, 60), '');
assert.equal(live.sparkPoints([5], 10, 300, 60), '0.0,30.0 300.0,30.0', 'one sample is a level line');
assert.equal(live.sparkPoints([0, 0], 0, 300, 60).split(' ')[1], '0.0,59.0', 'idle traffic rests on the floor');
assert.equal(live.areaPoints('10.0,5.0 300.0,1.0', 60), '10.0,60 10.0,5.0 300.0,1.0 300.0,60');
assert.equal(live.areaPoints('', 60), '');

assert.equal(live.fmtRate(0), '0 بایت/ثانیه');
assert.equal(live.fmtRate(1536), '1.5 کیلوبایت/ثانیه');
assert.equal(live.fmtRate(12.5 * 1024 * 1024), '12.5 مگ/ثانیه');
assert.equal(live.fmtRate(150 * 1024 * 1024), '150 مگ/ثانیه');
assert.equal(live.fmtRate(NaN), '—');
const pts = live.sparkPoints([0, 10], 10, 300, 60).split(' ');
assert.equal(pts.length, 2);
assert.equal(pts[1], '0.0,1.0', 'newest sample at the left edge, peak at the top');
assert.equal(pts[0].split(',')[0], (300 / (live.SAMPLES - 1)).toFixed(1));

assert.equal(live.badgeClass('ok'), 'badge connected');
assert.equal(live.badgeClass('<x>'), 'badge neutral');
assert.equal(live.backoffDelay(0), 0, 'a healthy poller keeps its normal pace');
assert.equal(live.backoffDelay(1), live.POLL_MS);
assert.equal(live.backoffDelay(2), live.POLL_MS * 2);
assert.equal(live.backoffDelay(3), live.POLL_MS * 4);
assert.equal(live.backoffDelay(50), live.MAX_BACKOFF_MS, 'backoff stops at the ceiling');
assert.equal(live.backoffDelay(NaN), 0);
console.log('overview-live ok');
