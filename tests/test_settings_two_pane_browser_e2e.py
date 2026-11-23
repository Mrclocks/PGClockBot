"""REAL browser E2E: settings pages on a desktop show the area's menu beside the page; the list page is tiles.

From 1200 the menu (settings_menu) sits beside the page, starting on the same line as the page's first block, and
the page's back button is hidden; below 1200 the menu is hidden and the back button returns. The settings list is
tiles of one height in fixed columns (two, four from 1400) from 900, and the phone list below.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "panel-ds.css", "panel-nav-modes.css")
HEAD = """<!doctype html><html lang="fa" dir="rtl" data-theme="dark"><head><meta charset="utf-8" />{links}</head>
<body><main class="main" id="main"><div class="main-body">{body}</div></main></body></html>"""
PAGE = """<div class="ds ds-settings">
<div class="page-head page-head--detail" data-page-head="flow"><div class="page-head-main">
  <a class="page-back" id="back" href="/settings" aria-label="بازگشت">‹</a><h1>هویت ربات</h1></div></div>
<nav class="settings-nav settings-nav--brand" id="menu" aria-label="صفحه‌های تنظیمات"><div class="settings-nav-group">
  <p class="settings-nav-title">ظاهر ربات</p>
  <a class="settings-nav-item" href="/settings?tab=identity" aria-current="page"><span class="settings-nav-ico"></span><span class="settings-nav-label">هویت ربات</span></a>
  <a class="settings-nav-item" href="/settings?tab=menu"><span class="settings-nav-ico"></span><span class="settings-nav-label">منوی ربات</span></a>
</div></nav>
<nav class="section-tabs settings-tabs settings-sections" id="tabs"><a class="active" href="#">خوش‌آمد</a><a href="#">پروفایل</a></nav>
<div class="settings-layout" id="block"><div class="settings-main"><div class="card">فرم</div></div></div>
</div>"""
LIST = """<div class="ds ds-settings"><div class="page-head" data-page-head="flow"><h1>تنظیمات ربات</h1></div>
<div class="settings-hub settings-hub--brand"><section class="hub-group"><h2 class="hub-group-title">ظاهر ربات</h2>
<div class="hub-list" id="list">{rows}</div></section></div></div>"""
ROW = """<a class="hub-row" href="#"><span class="hub-ico"></span><span class="hub-text"><span class="hub-label">صفحه {n}</span>
<span class="hub-desc">{desc}</span></span><span class="hub-chev"></span></a>"""

LAYOUT = """() => {
  const box = (id) => document.getElementById(id).getBoundingClientRect();
  const shown = (id) => getComputedStyle(document.getElementById(id)).display !== 'none';
  const menu = box('menu'), tabs = box('tabs'), block = box('block');
  return { menu: shown('menu'), back: shown('back'), menuTop: Math.round(menu.top), tabsTop: Math.round(tabs.top),
    menuWidth: Math.round(menu.width), apart: menu.left >= block.right || menu.right <= block.left,
    blockGap: Math.round(block.top - tabs.bottom) };
}"""
TILES = """() => {
  const list = document.getElementById('list');
  const tiles = [...list.children].map((t) => t.getBoundingClientRect());
  return { display: getComputedStyle(list).display, columns: getComputedStyle(list).gridTemplateColumns.split(' ').length,
    heights: [...new Set(tiles.map((t) => Math.round(t.height)))] };
}"""


class SettingsTwoPaneTests(unittest.TestCase):
    def _probe(self, body: str, width: int, script: str) -> dict:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        links = "".join(f'<link rel="stylesheet" href="{(STATIC / name).as_uri()}" />' for name in SHEETS)
        with tempfile.TemporaryDirectory() as tmp:
            page_file = Path(tmp) / "page.html"
            page_file.write_text(HEAD.format(links=links, body=body), encoding="utf-8")
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                try:
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    page.goto(page_file.as_uri())
                    return page.evaluate(script)
                finally:
                    browser.close()

    def test_a_desktop_page_has_the_menu_beside_it_on_the_first_line(self) -> None:
        for width in (1280, 1920):
            got = self._probe(PAGE, width, LAYOUT)
            self.assertTrue(got["menu"], width)
            self.assertFalse(got["back"], f"{width}: the menu replaces the back button")
            self.assertEqual(got["menuTop"], got["tabsTop"], f"{width}: menu and first block start together")
            self.assertEqual(got["menuWidth"], 272, width)
            self.assertTrue(got["apart"], f"{width}: the menu never covers the page")
            self.assertEqual(got["blockGap"], 16, f"{width}: blocks keep the page's 16 between them")

    def test_below_the_desktop_width_the_page_keeps_its_back_button(self) -> None:
        got = self._probe(PAGE, 1024, LAYOUT)
        self.assertFalse(got["menu"])
        self.assertTrue(got["back"])

    def test_the_list_is_tiles_of_one_height_in_fixed_columns(self) -> None:
        rows = "".join(
            ROW.format(n=n, desc="توضیح کوتاه" if n % 2 else "توضیحی بلندتر که در این عرض به دو خط می‌رسد و دو خط می‌شود")
            for n in range(4)
        )
        body = LIST.format(rows=rows)
        for width, columns in ((1000, 2), (1500, 4)):
            got = self._probe(body, width, TILES)
            self.assertEqual(got["display"], "grid", width)
            self.assertEqual(got["columns"], columns, width)
            self.assertEqual(len(got["heights"]), 1, f"{width}: every tile has the same height {got['heights']}")
        self.assertEqual(self._probe(body, 390, TILES)["display"], "flex", "phones keep the list")


if __name__ == "__main__":
    unittest.main()
