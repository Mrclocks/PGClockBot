"""Browser checks for list/tag/modal polish: gift-code button, sync button width, tag picker and modal padding.

Pages come from the real app (rendered by the ASGI app) and are shown by Chromium with the shipped
panel.css and panel.js.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from app.services.color_tags import COLOR_TAGS
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
MOBILE = {"width": 440, "height": 956}


def _launch(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001 - playwright raises a generic Error for a missing browser
        builds = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
        if not builds:
            raise
        return playwright.chromium.launch(headless=True, executable_path=str(builds[-1]))


class _Pages:
    """Serve fixed HTML pages and the real static files on a free port."""

    def __init__(self, pages: dict[str, str]) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
        for path, html in pages.items():
            app.add_api_route(path, (lambda body=html: HTMLResponse(body)), methods=["GET"])
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=lambda: asyncio.run(self._server.serve()), daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("page server failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _picker_page() -> str:
    env = Environment(loader=FileSystemLoader(str(WEB / "templates")), autoescape=True)
    picker = env.get_template("_color_tag_picker.html").render(
        color_tags=[{"key": t.key, "title_fa": t.title_fa, "hex": t.hex} for t in COLOR_TAGS], selected_color="yellow"
    )
    return (
        '<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/static/panel.css"></head>'
        f'<body><div id="box" style="width:272px">{picker}</div></body></html>'
    )


def _modal_page() -> str:
    return (
        '<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/static/panel.css"></head><body>'
        '<button id="open" data-modal-open="m1">باز</button>'
        '<div class="ui-modal" id="m1" hidden><div class="ui-modal-backdrop" data-modal-close></div>'
        '<div class="ui-modal-panel" role="dialog" aria-modal="true"><div class="ui-modal-head"><h2>عنوان</h2>'
        '<button class="icon-btn" data-modal-close>×</button></div>'
        '<form class="form-stack"><label class="form-field" id="field">فیلد<input></label>'
        '<div class="actions"><button class="btn" id="last" type="submit">ذخیره</button></div></form></div></div>'
        '<script src="/static/panel.js"></script></body></html>'
    )


def _settings_save_modal_page() -> str:
    """Domain-settings shape: one primary sticky save + a separate warn action."""
    lines = "".join(f'<p class="muted">خط تنظیمات {i}</p>' for i in range(36))
    return (
        '<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/static/panel.css"></head><body>'
        '<button id="open" data-modal-open="m1">باز</button>'
        '<div class="ui-modal" id="m1" hidden><div class="ui-modal-backdrop" data-modal-close></div>'
        '<div class="ui-modal-panel" role="dialog" aria-modal="true">'
        '<div class="ui-modal-head"><h2>تنظیمات</h2>'
        '<button class="icon-btn" data-modal-close>×</button></div>'
        '<div class="settings-domain-stack">'
        f'<form class="settings-form" action="#">{lines}'
        '<div class="actions sticky-actions ui-modal-sticky-save">'
        '<button class="btn" id="save" type="submit">ذخیره تنظیمات</button></div></form>'
        '<form class="settings-form" action="#">'
        '<div class="card settings-card"><h2>عمل جدا</h2></div>'
        '<div class="actions sticky-actions">'
        '<button class="btn btn-warn" id="warn" type="submit">لغو الان</button></div></form>'
        '</div></div></div>'
        '<script src="/static/panel.js"></script></body></html>'
    )


class AppPagesCase(RoutesHarness):
    async def fetch(self, path: str) -> str:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        response = await self.client.get(path, headers={"cookie": cookie})
        self.assertEqual(response.status_code, 200, path)
        return response.text


class PlansPageTests(AppPagesCase):
    async def test_the_plans_page_has_no_gift_code_button(self):
        html = await self.fetch("/plans")
        self.assertNotIn('href="/gift-codes"', html)


class BrowserTests(AppPagesCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc

    def _with_browser(self, pages: dict[str, str], check) -> None:
        from playwright.sync_api import sync_playwright

        server = _Pages(pages)
        server.start()
        try:
            with sync_playwright() as p:
                browser = _launch(p)
                check(browser, f"http://127.0.0.1:{server.port}")
                browser.close()
        finally:
            server.stop()

    async def test_the_repair_button_spans_the_card_on_a_phone(self):
        with patch("app.services.representative_unification.assert_live_parent_for_child", AsyncMock()):
            html = await self.fetch("/resellers")

        def check(browser, base):
            page = browser.new_page(viewport=MOBILE)
            page.goto(base + "/resellers")
            sizes = page.evaluate(
                """() => { const b = document.querySelector('form[action="/resellers/repair-capacity"] button');
                  const form = b.closest('form').getBoundingClientRect(); const r = b.getBoundingClientRect();
                  return [Math.round(r.width), Math.round(form.width)]; }"""
            )
            self.assertEqual(sizes[0], sizes[1], sizes)

        await asyncio.to_thread(self._with_browser, {"/resellers": html}, check)

    def test_the_tag_picker_has_no_circle_colours_each_option_and_fits_one_row(self):
        def check(browser, base):
            page = browser.new_page(viewport=MOBILE)
            page.goto(base + "/picker")
            self.assertEqual(page.locator(".color-tag-swatch").count(), 0)
            rows = page.evaluate(
                "() => new Set([...document.querySelectorAll('.color-tag-option')].map(e => Math.round(e.getBoundingClientRect().top))).size"
            )
            self.assertEqual(rows, 1)
            colours = page.evaluate(
                "() => [...document.querySelectorAll('.color-tag-label')].map(e => getComputedStyle(e).color)"
            )
            expected = ["rgb(%d, %d, %d)" % tuple(int(t.hex[i : i + 2], 16) for i in (1, 3, 5)) for t in COLOR_TAGS]
            self.assertEqual(colours, expected)

        self._with_browser({"/picker": _picker_page()}, check)

    def test_a_modal_has_the_same_air_above_and_below_as_beside_its_content(self):
        def check(browser, base):
            page = browser.new_page(viewport=MOBILE)
            page.goto(base + "/modal")
            page.click("#open")
            page.wait_for_selector("#m1.open")
            page.wait_for_timeout(400)
            gaps = page.evaluate(
                """() => { const panel = document.querySelector('#m1 .ui-modal-panel').getBoundingClientRect();
                  const scroll = document.querySelector('#m1 .ui-modal-scroll');
                  const gutter = scroll.offsetWidth - scroll.clientWidth;
                  /* Sticky head sits outside the scrollport; measure to the title
                     text (not the head box edge) so top air matches side padding. */
                  const title = document.querySelector('#m1 .ui-modal-head h2').getBoundingClientRect();
                  const last = document.getElementById('last').getBoundingClientRect();
                  const field = document.getElementById('field').getBoundingClientRect();
                  return {top: title.top - panel.top, bottom: panel.bottom - last.bottom, side: field.left - panel.left - gutter / 2}; }"""
            )
            self.assertAlmostEqual(gaps["top"], gaps["side"], delta=1.5, msg=gaps)
            self.assertAlmostEqual(gaps["bottom"], gaps["side"], delta=1.5, msg=gaps)

        self._with_browser({"/modal": _modal_page()}, check)

    def test_modal_title_stays_fixed_while_body_scrolls(self):
        """Sticky frosted header: head is sticky inside .ui-modal-scroll."""

        def check(browser, base):
            page = browser.new_page(viewport=MOBILE)
            page.goto(base + "/modal")
            page.click("#open")
            page.wait_for_selector("#m1.open")
            page.wait_for_timeout(400)
            layout = page.evaluate(
                """() => {
                  const panel = document.querySelector('#m1 .ui-modal-panel');
                  const scroll = panel.querySelector(':scope > .ui-modal-scroll');
                  const head = scroll && scroll.querySelector(':scope > .ui-modal-head');
                  const form = document.querySelector('#m1 form');
                  if (!head || !scroll || !form) return {ok: false};
                  const style = getComputedStyle(head);
                  const before = head.getBoundingClientRect().top;
                  scroll.scrollTop = Math.min(160, scroll.scrollHeight);
                  const after = head.getBoundingClientRect().top;
                  const pb = parseFloat(style.paddingBottom) || 0;
                  return {
                    ok: true,
                    headInsideScroll: scroll.contains(head),
                    formInsideScroll: scroll.contains(form),
                    sticky: style.position === 'sticky',
                    headFixed: Math.abs(before - after) < 1.5,
                    paddingBottom: pb,
                    frosted: (style.backdropFilter || style.webkitBackdropFilter || '').includes('blur'),
                  };
                }"""
            )
            self.assertTrue(layout["ok"], msg=layout)
            self.assertTrue(layout["headInsideScroll"], msg=layout)
            self.assertTrue(layout["formInsideScroll"], msg=layout)
            self.assertTrue(layout["sticky"], msg=layout)
            self.assertTrue(layout["headFixed"], msg=layout)
            self.assertTrue(layout["frosted"], msg=layout)
            self.assertGreaterEqual(layout["paddingBottom"], 15, msg=layout)

        tall = _modal_page().replace(
            '<form class="form-stack">',
            '<form class="form-stack">' + "".join(f'<p class="muted">خط {i}</p>' for i in range(40)),
        )
        self._with_browser({"/modal": tall}, check)

    def test_single_save_stays_fixed_while_warn_action_scrolls(self):
        """Primary save is a panel footer; warn actions stay in the scroll body."""

        def check(browser, base):
            page = browser.new_page(viewport=MOBILE)
            page.goto(base + "/modal")
            page.click("#open")
            page.wait_for_selector("#m1.open")
            page.wait_for_timeout(400)
            layout = page.evaluate(
                """() => {
                  const panel = document.querySelector('#m1 .ui-modal-panel');
                  const scroll = panel.querySelector(':scope > .ui-modal-scroll');
                  const saveRow = document.querySelector('#save').closest('.ui-modal-sticky-save');
                  const warnRow = document.querySelector('#warn').closest('.sticky-actions');
                  const head = document.querySelector('#m1 .ui-modal-head');
                  if (!panel || !scroll || !saveRow || !warnRow || !head) return {ok: false};
                  const saveStyle = getComputedStyle(saveRow);
                  const headStyle = getComputedStyle(head);
                  const scrollPadBottom = parseFloat(getComputedStyle(scroll).paddingBottom) || 0;
                  scroll.scrollTop = 0;
                  const saveTopAtStart = saveRow.getBoundingClientRect().top;
                  const warnTopAtStart = warnRow.getBoundingClientRect().top;
                  scroll.scrollTop = Math.min(220, scroll.scrollHeight);
                  const saveTopScrolled = saveRow.getBoundingClientRect().top;
                  const warnTopScrolled = warnRow.getBoundingClientRect().top;
                  const panelBox = panel.getBoundingClientRect();
                  const saveBox = saveRow.getBoundingClientRect();
                  const saveBtn = document.getElementById('save').getBoundingClientRect();
                  const savePad = parseFloat(saveStyle.paddingLeft) || 0;
                  const padTop = parseFloat(saveStyle.paddingTop) || 0;
                  const padBottom = parseFloat(saveStyle.paddingBottom) || 0;
                  scroll.scrollTop = scroll.scrollHeight;
                  const warnBottomAtEnd = warnRow.getBoundingClientRect().bottom;
                  return {
                    ok: true,
                    saveIsPanelChild: saveRow.parentElement === panel,
                    saveOutsideScroll: !scroll.contains(saveRow),
                    warnInsideScroll: scroll.contains(warnRow),
                    saveFrosted: (saveStyle.backdropFilter || saveStyle.webkitBackdropFilter || '').includes('blur'),
                    sameFrost: saveStyle.backgroundColor === headStyle.backgroundColor,
                    savePinned: Math.abs(saveBox.bottom - panelBox.bottom) < 2.5,
                    saveFixedWhileScroll: Math.abs(saveTopScrolled - saveTopAtStart) < 1.5,
                    warnMoved: Math.abs(warnTopScrolled - warnTopAtStart) > 40,
                    saveFullWidth: Math.abs(saveBtn.width - (saveBox.width - 2 * savePad)) < 2,
                    saveVerticallyCentered: Math.abs(padTop - padBottom) < 0.5,
                    savePadY: padTop,
                    saveOverlays: saveStyle.position === 'absolute',
                    scrollKeepsBottomPad: scrollPadBottom >= 60,
                    warnNotFlushAtEnd: (panelBox.bottom - warnBottomAtEnd) >= 60,
                  };
                }"""
            )
            self.assertTrue(layout["ok"], msg=layout)
            self.assertTrue(layout["saveIsPanelChild"], msg=layout)
            self.assertTrue(layout["saveOutsideScroll"], msg=layout)
            self.assertTrue(layout["warnInsideScroll"], msg=layout)
            self.assertTrue(layout["saveFrosted"], msg=layout)
            self.assertTrue(layout["sameFrost"], msg=layout)
            self.assertTrue(layout["savePinned"], msg=layout)
            self.assertTrue(layout["saveFixedWhileScroll"], msg=layout)
            self.assertTrue(layout["warnMoved"], msg=layout)
            self.assertTrue(layout["saveFullWidth"], msg=layout)
            self.assertTrue(layout["saveVerticallyCentered"], msg=layout)
            self.assertTrue(layout["saveOverlays"], msg=layout)
            self.assertGreaterEqual(layout["savePadY"], 18, msg=layout)
            self.assertTrue(layout["scrollKeepsBottomPad"], msg=layout)
            self.assertTrue(layout["warnNotFlushAtEnd"], msg=layout)

        self._with_browser({"/modal": _settings_save_modal_page()}, check)
