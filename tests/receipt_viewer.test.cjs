const test = require('node:test');
const assert = require('node:assert/strict');

global.window = { PanelNumbers: require('../app/web/static/fa-number.js') };
const viewer = require('../app/web/static/receipt-viewer.js');

test('the photo count is a whole number from one, else one', () => {
  assert.equal(viewer.readCount('2'), 2);
  assert.equal(viewer.readCount(' 3 '), 3);
  for (const bad of [null, '', '0', '-1', '1.5', 'x', '<b>']) assert.equal(viewer.readCount(bad), 1, String(bad));
});

test('photo n is the receipt URL with its index and a cache breaker', () => {
  assert.equal(viewer.photoUrl('/payments/7/receipt', 0, 5), '/payments/7/receipt?n=0&_=5');
  assert.equal(viewer.photoUrl('/payments/7/receipt?x=1', 1, 5), '/payments/7/receipt?x=1&n=1&_=5');
});

test('the counter reads in Persian digits', () => {
  assert.equal(viewer.counterText(0, 2), 'تصویر ۱ از ۲');
  assert.equal(viewer.counterText(1, 2), 'تصویر ۲ از ۲');
});
