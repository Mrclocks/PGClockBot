// The navigation clock must not stay on screen when a form submit is cancelled
// (for example by the Persian required-field validator); a real submit still shows it.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let chromium = null;
try {
  ({ chromium } = require('playwright'));
} catch (_) {
  chromium = null;
}

const PANEL_JS = fs.readFileSync(path.join(__dirname, '../app/web/static/panel.js'), 'utf8');
const PAGE = `<!doctype html><html><body>
  <div id="panel-nav-clock" hidden aria-hidden="true"></div>
  <form method="post" action="/reject">
    <label class="form-field">علت رد<textarea name="note" required></textarea></label>
    <button id="send" type="submit">رد درخواست</button>
  </form></body></html>`;

async function openPage(browser, onPost) {
  const page = await browser.newPage();
  await page.route('http://panel.test/', (route) => route.fulfill({ contentType: 'text/html', body: PAGE }));
  await page.route('http://panel.test/reject', onPost);
  await page.goto('http://panel.test/');
  await page.addScriptTag({ content: PANEL_JS });
  await page.evaluate(() => document.dispatchEvent(new Event('DOMContentLoaded')));
  return page;
}

const clockHidden = (page) => page.$eval('#panel-nav-clock', (clock) => clock.hidden);

test('a submit the validator blocks does not leave the clock spinning', { skip: !chromium }, async () => {
  const browser = await chromium.launch();
  try {
    const page = await openPage(browser, (route) => route.abort());
    await page.click('#send');
    await page.waitForTimeout(100);
    assert.equal(await clockHidden(page), true);
    assert.equal(await page.$$eval('.field-error', (nodes) => nodes.length), 1);
  } finally {
    await browser.close();
  }
});

test('a valid submit keeps the clock while the server answers', { skip: !chromium }, async () => {
  const browser = await chromium.launch();
  try {
    const page = await openPage(browser, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.fulfill({ contentType: 'text/html', body: 'ok' }).catch(() => {});
    });
    // The page is mid-navigation after a real submit, so it reports the clock itself.
    let reported;
    const seen = new Promise((resolve) => { reported = resolve; });
    await page.exposeFunction('reportClockHidden', (hidden) => reported(hidden));
    await page.evaluate(() => {
      document.addEventListener('submit', () => {
        setTimeout(() => window.reportClockHidden(document.getElementById('panel-nav-clock').hidden), 50);
      });
    });
    await page.fill('textarea[name="note"]', 'تکراری است');
    await page.click('#send', { noWaitAfter: true });
    assert.equal(await seen, false);
  } finally {
    await browser.close();
  }
});
