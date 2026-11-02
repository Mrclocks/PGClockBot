const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness(fetch) {
  let deadline;
  let cleared = false;
  const context = {
    module: { exports: {} }, AbortController, fetch,
    setTimeout(callback, ms) { assert.equal(ms, 8000); deadline = callback; return 1; },
    clearTimeout(id) { assert.equal(id, 1); cleared = true; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/web/static/panel_widget_request.js'), 'utf8'), context);
  return { api: context.module.exports, expire: () => deadline(), cleared: () => cleared };
}

test('successful fragment read clears its deadline and preserves safe fetch options', async () => {
  const h = harness(async (url, options) => {
    assert.equal(url, '/home/body');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.redirect, 'manual');
    assert.equal(options.signal.aborted, false);
    return { ok: true, status: 200, text: async () => '<div id="home-dash">local stats</div>' };
  });
  const body = await h.api.fetchBody('/home/body', { credentials: 'same-origin', redirect: 'manual' });
  assert.match(body, /local stats/);
  assert.equal(h.cleared(), true);
});

test('a hung fetch is bounded even when it ignores the abort signal', async () => {
  let signal;
  const h = harness((_url, options) => { signal = options.signal; return new Promise(() => {}); });
  const read = h.api.fetchBody('/home/body', {});
  await Promise.resolve();
  h.expire();
  await assert.rejects(read, /widget-timeout/);
  assert.equal(signal.aborted, true);
  assert.equal(h.cleared(), true);
});

test('deadline includes downloading the response body', async () => {
  const h = harness(async () => ({ ok: true, status: 200, text: () => new Promise(() => {}) }));
  const read = h.api.fetchBody('/pg/body', {});
  await Promise.resolve();
  h.expire();
  await assert.rejects(read, /widget-timeout/);
  assert.equal(h.cleared(), true);
});

for (const status of [301, 302, 303, 307, 308, 401, 403]) {
  test(`status ${status} never inserts login HTML into a dashboard`, async () => {
    let bodyRead = false;
    const h = harness(async () => ({ ok: false, status, text() { bodyRead = true; return 'login page'; } }));
    await assert.rejects(h.api.fetchBody('/home/body', {}), /widget-auth/);
    assert.equal(bodyRead, false);
    assert.equal(h.cleared(), true);
  });
}

test('opaque redirect does not insert a login page', async () => {
  const h = harness(async () => ({ type: 'opaqueredirect', status: 0 }));
  await assert.rejects(h.api.fetchBody('/home/body', {}), /widget-auth/);
});

test('server failure is reported without exposing its response text', async () => {
  const h = harness(async () => ({ ok: false, status: 500, text() { throw new Error('private provider data'); } }));
  await assert.rejects(h.api.fetchBody('/home/body', {}), /widget-response/);
  assert.equal(h.cleared(), true);
});

test('a failed read can be retried once the connection recovers', async () => {
  let attempt = 0;
  const h = harness(async () => {
    if (++attempt === 1) throw new Error('network down');
    return { ok: true, status: 200, text: async () => 'recovered' };
  });
  await assert.rejects(h.api.fetchBody('/home/body', {}), /network down/);
  assert.equal(await h.api.fetchBody('/home/body', {}), 'recovered');
  assert.equal(attempt, 2);
});

test('live metrics use the same deadline and do not follow redirects', async () => {
  const h = harness(async (_url, options) => {
    assert.equal(options.redirect, 'manual');
    return { ok: true, status: 200, json: async () => ({ cpu_percent: 12 }) };
  });
  assert.equal((await h.api.fetchJson('/pg/metrics', {})).cpu_percent, 12);
  assert.equal(h.cleared(), true);
});

test('live metrics never overlap while a previous read is pending', async () => {
  const callbacks = {};
  let complete;
  let calls = 0;
  const liveRoot = { getAttribute: () => '/pg/metrics' };
  const context = {
    document: {
      hidden: false,
      readyState: 'complete',
      getElementById: () => null,
      querySelectorAll: () => [],
      querySelector: selector => (selector === '[data-pg-live][data-metrics-url]' ? liveRoot : null),
      addEventListener: (name, callback) => { callbacks[name] = callback; },
    },
    PGClockPanelWidgetRequest: {
      fetchJson: () => { calls++; return new Promise(resolve => { complete = resolve; }); },
    },
    setInterval: () => {}, setTimeout: () => {}, Date, Math, isFinite, Number, String,
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/web/static/overview-live.js'), 'utf8'), context);
  callbacks['panel-widgets-ready']();
  callbacks.visibilitychange();
  assert.equal(calls, 1);
  complete({});
  await new Promise(resolve => setImmediate(resolve));
  callbacks['panel-widgets-ready']();
  assert.equal(calls, 2);
  complete({});
});

test('partial metrics are reported as unavailable instead of replacing the last sample', async () => {
  let readBody = false;
  const h = harness(async () => ({
    ok: true, status: 200, headers: { get: () => 'timeout' },
    json: async () => { readBody = true; return {}; },
  }));
  await assert.rejects(h.api.fetchJson('/pg/metrics', {}), /widget-unavailable/);
  assert.equal(readBody, false);
});

test('widget reads never hold the navigation clock after pageshow', () => {
  const listeners = { document: {}, window: {} };
  const attributes = new Map([['data-panel-widgets-pending', '1']]);
  const clock = { hidden: true, setAttribute() {}, offsetWidth: 0 };
  const assigned = [];
  const context = {
    side: null, setOpen() {}, URL,
    location: { origin: 'https://panel.test', href: 'https://panel.test/home', pathname: '/home', search: '' },
    requestAnimationFrame: callback => callback(),
    document: {
      getElementById: () => clock,
      documentElement: {
        getAttribute: key => attributes.get(key),
        setAttribute: (key, value) => attributes.set(key, value),
        removeAttribute: key => attributes.delete(key),
      },
      addEventListener: (name, callback) => { listeners.document[name] = callback; },
    },
    window: {
      location: { assign: url => assigned.push(url) },
      addEventListener: (name, callback) => { listeners.window[name] = callback; },
    },
  };
  const source = fs.readFileSync(path.join(__dirname, '../app/web/static/panel.js'), 'utf8');
  const block = source.slice(source.indexOf('/* Lightweight nav clock'), source.indexOf('/* Permanent no-zoom'));
  vm.runInNewContext(block.slice(block.indexOf('(function ()')), context);
  listeners.document['panel-widgets-loading']();
  assert.equal(clock.hidden, true);
  let prevented = false;
  listeners.document.click({
    button: 0, preventDefault: () => { prevented = true; },
    target: { closest: () => ({
      target: '', href: 'https://panel.test/plans', hasAttribute: () => false,
      getAttribute: key => key === 'href' ? '/plans' : null,
    }) },
  });
  assert.equal(prevented, true);
  assert.equal(clock.hidden, false);
  assert.deepEqual(assigned, ['https://panel.test/plans']);
  listeners.document['panel-widgets-ready']();
  assert.equal(clock.hidden, false);
  listeners.document['panel-widgets-loading']();
  listeners.window.pageshow();
  assert.equal(attributes.get('data-panel-widgets-pending'), '1');
  assert.equal(clock.hidden, true);
});
