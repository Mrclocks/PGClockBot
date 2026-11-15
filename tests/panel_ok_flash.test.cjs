const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadApi() {
  const context = { module: { exports: {} }, URL };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '../app/web/static/panel_ok_flash.js'), 'utf8'),
    context,
  );
  return context.module.exports;
}

function makeFlash(className) {
  const node = {
    className,
    parentNode: null,
    remove() {
      if (!this.parentNode) return;
      this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
      this.parentNode = null;
    },
  };
  return node;
}

function makeDoc(children) {
  const mainBody = { children: children.slice() };
  children.forEach((child) => { child.parentNode = mainBody; });
  return {
    querySelector(sel) {
      if (sel === '.main-body > .flash.ok') {
        return mainBody.children.find((child) => child.className === 'flash ok') || null;
      }
      return null;
    },
  };
}

function makeLocation(href) {
  const url = new URL(href);
  return {
    get href() { return url.href; },
    get pathname() { return url.pathname; },
    get search() { return url.search; },
    get hash() { return url.hash; },
  };
}

function makeHistory(loc) {
  return {
    replaceState(_state, _title, next) {
      const resolved = new URL(next, 'https://panel.example');
      Object.defineProperty(loc, 'href', { get: () => resolved.href, configurable: true });
      Object.defineProperty(loc, 'pathname', { get: () => resolved.pathname, configurable: true });
      Object.defineProperty(loc, 'search', { get: () => resolved.search, configurable: true });
      Object.defineProperty(loc, 'hash', { get: () => resolved.hash, configurable: true });
    },
  };
}

test('page success flash dismisses after the upload-toast delay and drops ?ok=', () => {
  const api = loadApi();
  const flash = makeFlash('flash ok');
  const doc = makeDoc([flash]);
  const loc = makeLocation(
    'https://panel.example/home?ok=%D8%A7%D8%AA%D8%B5%D8%A7%D9%84%20%D8%A8%D8%B1%D9%82%D8%B1%D8%A7%D8%B1%20%D8%B4%D8%AF%20(12%20ms)&tab=1',
  );
  const hist = makeHistory(loc);
  let fired;
  const schedule = (fn, ms) => {
    assert.equal(ms, api.DISMISS_MS);
    assert.equal(ms, 2800);
    fired = fn;
    return 1;
  };
  assert.equal(api.scheduleDismiss(doc, loc, hist, schedule), 1);
  assert.equal(api.pageOkFlash(doc), flash);
  fired();
  assert.equal(api.pageOkFlash(doc), null);
  assert.equal(loc.pathname + loc.search, '/home?tab=1');
});

test('missing page success flash is a no-op and leaves the URL alone', () => {
  const api = loadApi();
  const err = makeFlash('flash err');
  const doc = makeDoc([err]);
  const loc = makeLocation('https://panel.example/home?ok=1');
  const hist = makeHistory(loc);
  let scheduled = false;
  const schedule = () => { scheduled = true; return 1; };
  assert.equal(api.scheduleDismiss(doc, loc, hist, schedule), null);
  assert.equal(scheduled, false);
  assert.equal(loc.search, '?ok=1');
  assert.equal(err.parentNode !== null, true);
});

test('nested modal success flashes are not treated as page flashes', () => {
  const api = loadApi();
  const doc = {
    querySelector(sel) {
      assert.equal(sel, '.main-body > .flash.ok');
      return null;
    },
  };
  assert.equal(api.pageOkFlash(doc), null);
});
