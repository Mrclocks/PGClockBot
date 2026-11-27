const test = require('node:test');
const assert = require('node:assert/strict');

global.window = { PanelNumbers: require('../app/web/static/fa-number.js') };
const badge = require('../app/web/static/inbox-badge.js');

function makeLink(withBadge) {
  const children = [];
  const doc = {
    createElement() {
      const node = {
        className: '',
        textContent: '',
        attrs: {},
        setAttribute(name, value) { this.attrs[name] = value; },
        remove() { children.splice(children.indexOf(node), 1); },
      };
      return node;
    },
  };
  const link = {
    ownerDocument: doc,
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    appendChild(node) { children.push(node); },
    querySelector(sel) {
      return sel === '.topbar-inbox-count' ? children.find((n) => n.className === 'topbar-inbox-count') || null : null;
    },
    children,
  };
  if (withBadge) {
    const node = doc.createElement('span');
    node.className = 'topbar-inbox-count';
    node.textContent = '۱';
    link.appendChild(node);
  }
  return link;
}

test('readCount accepts only whole non-negative numbers', () => {
  assert.equal(badge.readCount('0'), 0);
  assert.equal(badge.readCount(' 3 '), 3);
  assert.equal(badge.readCount(null), null);
  assert.equal(badge.readCount(''), null);
  assert.equal(badge.readCount('-1'), null);
  assert.equal(badge.readCount('1.5'), null);
  assert.equal(badge.readCount('<b>'), null);
});

test('a stale badge is removed when the fresh list is empty', () => {
  const link = makeLink(true);
  badge.applyCount(link, 0);
  assert.equal(link.querySelector('.topbar-inbox-count'), null);
  assert.equal(link.attrs['aria-label'], 'مرکز اقدام');
});

test('the badge is created or updated with Persian digits and capped at 99+', () => {
  const link = makeLink(false);
  badge.applyCount(link, 2);
  assert.equal(link.querySelector('.topbar-inbox-count').textContent, '۲');
  assert.equal(link.attrs['aria-label'], 'مرکز اقدام، ۲ اعلان');
  badge.applyCount(link, 150);
  assert.equal(link.children.length, 1);
  assert.equal(link.querySelector('.topbar-inbox-count').textContent, '۹۹+');
});
