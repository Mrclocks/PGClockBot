"""REAL browser E2E: the desktop sidebar brand matches the phone header — round logo on the left, name beside it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "panel-nav-modes.css", "panel-ds.css")
PAGE = """<!doctype html><html lang="fa" dir="rtl" data-theme="dark"><head><meta charset="utf-8" />{links}</head>
<body><aside class="side" id="sidebar"><div class="side-panel">
<a class="brand desk-only" href="#"><span class="brand-logo" id="logo"><img alt="" width="40" height="40" /></span>
<div><strong id="name">MrClockBot</strong></div></a></div></aside></body></html>"""


class SideBrandTests(unittest.TestCase):
    def test_the_logo_is_round_and_left_of_the_name_on_desktop(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        links = "".join(f'<link rel="stylesheet" href="{(STATIC / name).as_uri()}" />' for name in SHEETS)
        with tempfile.TemporaryDirectory() as tmp:
            page_file = Path(tmp) / "page.html"
            page_file.write_text(PAGE.format(links=links), encoding="utf-8")
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 800})
                    page.goto(page_file.as_uri())
                    found = page.evaluate(
                        "() => { const logo = document.getElementById('logo');"
                        " const name = document.getElementById('name').getBoundingClientRect();"
                        " return { radius: getComputedStyle(logo).borderRadius,"
                        " logoRight: logo.getBoundingClientRect().right, nameLeft: name.left }; }"
                    )
                finally:
                    browser.close()
        self.assertEqual(found["radius"], "50%")
        self.assertLessEqual(found["logoRight"], found["nameLeft"])


if __name__ == "__main__":
    unittest.main()
