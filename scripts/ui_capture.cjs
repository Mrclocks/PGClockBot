#!/usr/bin/env node
/* Panel UI capture for before/after comparison (used by scripts/ui_regression.sh).
   Logs in, opens every page reachable from the start list and the side menu, the same-page views
   their links lead to (tabs, settings sections) and every modal a button opens. For each view, at
   every requested width and theme, it saves:
     <out>/png/<view>@<width>-<theme>.png   full-page screenshot, live server meters masked
     <out>/inventory.json                    the view's controls: text and target of every link and
                                             button, every form's method, action and field names
   Pages that change on their own between two runs (server meters) are masked, nothing else is.
   Usage: node scripts/ui_capture.cjs --base http://127.0.0.1:9311 --user uiadmin --pass '...' --out DIR
          [--widths 390,1440] [--themes light,dark] [--max 300] */
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith('--')) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : '1']);
  return acc; }, []));
const BASE = (args.base || 'http://127.0.0.1:9311').replace(/\/$/, '');
const OUT = args.out || 'ui-capture';
const WIDTHS = (args.widths || '390,1440').split(',').map(Number);
const THEMES = (args.themes || 'light,dark').split(',');
const MAX_VIEWS = +(args.max || 300);
const SEED = ['/home', '/dashboard', '/users', '/users/11/edit', '/plans', '/plans/1/edit', '/finance', '/tickets',
  '/inbox', '/resellers', '/resellers/2/edit', '/marketing', '/settings', '/connection-status', '/security', '/pg'];
const SKIP = /logout|export|csv|download|delete|remove|purge|reset|restart|update\/|backup\/(create|restore)|\/run|setup/i;
// Content that differs on every load by design: live figures are masked, the random
// example service name gets a fixed text (its length moves what sits next to it).
const MASK = ['[data-server-meters]', '[data-live]',
  // Figures measured by the running process: job run times, panel-link timings, the report's own clock.
  'section[aria-label="کارهای پس‌زمینه"] .table-wrap', '.home-link-health .link-health-summary',
  '.home-link-health .link-health-grid', '.finance-report-stamp'];
const FIXED_TEXT = { '#naming-sample': 'clk_sample01', '[data-naming-sample]': 'clk_sample01' };
// Chromium does not paint blurred backdrops and fading scrollbars the same way twice. Both runs
// screenshot without them; everything else on the page is drawn as served.
const STEADY_CSS = `*, *::before, *::after { backdrop-filter: none !important; -webkit-backdrop-filter: none !important;
  scrollbar-width: none !important; caret-color: transparent !important; }
  ::-webkit-scrollbar { display: none !important; }`;

const slug = (u) => (u.replace(/^\//, '').replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_|_$/g, '') || 'root').slice(0, 120);

async function inventory(p) {
  return p.evaluate(() => {
    const root = [...document.querySelectorAll('.ui-modal')].find((m) => !m.hidden && m.classList.contains('open')) || document.body;
    const text = (el) => (el.getAttribute('aria-label') || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const controls = [...root.querySelectorAll('a[href], button, [role="button"], input[type="submit"]')]
      .filter((el) => !el.closest('[hidden]'))
      .map((el) => [el.tagName.toLowerCase(), text(el), el.getAttribute('href') || el.getAttribute('data-modal-open') ||
        el.getAttribute('formaction') || el.getAttribute('type') || '', el.disabled ? 'disabled' : ''].join(' | '));
    const forms = [...root.querySelectorAll('form')].map((f) => ({
      method: (f.getAttribute('method') || 'get').toLowerCase(),
      action: f.getAttribute('action') || '',
      fields: [...f.elements].map((e) => e.name).filter((n) => n && n !== 'csrf_token').sort(),
    }));
    return { controls, forms };
  });
}

// The panel scrolls inside its own container, so a full-page shot alone stops at the window. The
// window is grown to the tallest scrolled content for the shot (same rule on both runs), then restored.
const MAX_SHOT_HEIGHT = 12000;
async function contentHeight(p) {
  return p.evaluate(() => Math.max(document.documentElement.scrollHeight,
    ...[...document.querySelectorAll('body *')].filter((el) => {
      const style = getComputedStyle(el);
      return /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 1;
    }).map((el) => el.scrollHeight + el.getBoundingClientRect().top)));
}

async function steady(p) {
  await p.addStyleTag({ content: STEADY_CSS });
  await p.evaluate((fixed) => {
    for (const [selector, text] of Object.entries(fixed)) document.querySelectorAll(selector).forEach((el) => { el.textContent = text; });
  }, FIXED_TEXT);
}

async function shoot(p, view, width) {
  const base = p.viewportSize();
  await steady(p);
  for (const theme of THEMES) {
    await p.evaluate((t) => { localStorage.setItem('panel-theme', t); document.documentElement.setAttribute('data-theme', t); }, theme);
    const height = Math.min(MAX_SHOT_HEIGHT, Math.max(base.height, Math.ceil(await contentHeight(p))));
    await p.setViewportSize({ width: base.width, height });
    // The pointer stays where the last click was; park it so no control shows a hover state.
    await p.mouse.move(0, 0);
    await p.waitForTimeout(150);
    const masks = MASK.map((s) => p.locator(s));
    await p.screenshot({ path: path.join(OUT, 'png', `${slug(view)}@${width}-${theme}.png`), fullPage: true,
      animations: 'disabled', caret: 'hide', mask: masks, maskColor: '#ff00ff' });
    await p.setViewportSize(base);
  }
}

async function run(width) {
  const browser = await chromium.launch();
  const mobile = width < 900;
  const ctx = await browser.newContext({ viewport: { width, height: mobile ? 844 : 1000 }, isMobile: mobile,
    hasTouch: mobile, reducedMotion: 'reduce', locale: 'fa-IR', timezoneId: 'Asia/Tehran' });
  const p = await ctx.newPage();
  await p.goto(BASE + '/login');
  await p.fill('input[name=username]', args.user || 'uiadmin');
  await p.fill('input[name=password]', args.pass || '');
  await Promise.all([p.waitForNavigation().catch(() => null), p.press('input[name=password]', 'Enter')]);
  const queue = [...SEED]; const seen = new Set(); const views = {};
  while (queue.length && seen.size < MAX_VIEWS) {
    const u = queue.shift(); if (seen.has(u)) continue; seen.add(u);
    const resp = await p.goto(BASE + u, { waitUntil: 'networkidle' }).catch(() => null);
    await p.evaluate(() => document.fonts && document.fonts.ready).catch(() => null);
    if (!resp || resp.status() >= 400) { views[u] = { status: resp ? resp.status() : 'error' }; continue; }
    const here = new URL(p.url());
    if (here.pathname === '/login') { views[u] = { status: 'login' }; continue; }
    const links = await p.evaluate(() => [...document.querySelectorAll('a[href]')].map((a) => a.getAttribute('href')));
    for (const h of links) {
      if (!h || !h.startsWith('/') || h.startsWith('//') || SKIP.test(h) || h.includes('#')) continue;
      const nu = new URL(h, BASE); const key = nu.pathname + nu.search;
      const samePage = nu.pathname === here.pathname;
      const menuPage = !nu.search && !/\/\d+/.test(nu.pathname);
      if ((samePage || menuPage) && !seen.has(key) && !queue.includes(key)) queue.push(key);
    }
    views[u] = { status: resp.status(), ...(await inventory(p)) };
    await shoot(p, u, width);
    const openers = await p.$$('[data-modal-open]'); const done = new Set();
    for (const o of openers) {
      try {
        const id = await o.getAttribute('data-modal-open');
        if (done.has(id) || !(await o.isVisible())) continue; done.add(id);
        await o.click({ timeout: 1500 }); await p.waitForTimeout(600);
        const key = `${u} #${id}`;
        views[key] = await inventory(p);
        await shoot(p, key, width);
        await p.keyboard.press('Escape'); await p.waitForTimeout(200);
        await p.evaluate(() => document.querySelectorAll('.ui-modal.open').forEach((m) => m.classList.remove('open')));
      } catch (e) { /* a control that cannot be clicked at this width is recorded by its absence */ }
    }
  }
  await browser.close();
  return views;
}

(async () => {
  fs.mkdirSync(path.join(OUT, 'png'), { recursive: true });
  const all = {};
  for (const width of WIDTHS) all[width] = await run(width);
  fs.writeFileSync(path.join(OUT, 'inventory.json'), JSON.stringify(all, null, 1));
  const counts = WIDTHS.map((w) => `${w}px: ${Object.keys(all[w]).length} views`).join(', ');
  console.log(`ui-capture ${OUT}: ${counts}, ${fs.readdirSync(path.join(OUT, 'png')).length} screenshots`);
})();
