"""REAL browser E2E: a password field lines up with the plain field beside it inside the design-system scope."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "panel-nav-modes.css", "panel-ds.css")
PAGE = """<!doctype html><html lang="fa" dir="rtl" data-theme="dark"><head><meta charset="utf-8" />{links}</head>
<body><main class="main ds"><div class="settings-grid">
<div class="setting-item"><label>نام کاربری<input id="user" name="u" dir="ltr" /></label></div>
<div class="setting-item"><label>رمز<div class="pw-field"><input id="pass" name="p" type="password" dir="ltr" /></div></label></div>
</div></main></body></html>"""


class FieldAlignmentTests(unittest.TestCase):
    def test_user_and_password_boxes_start_at_the_same_height(self) -> None:
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
                    tops = page.evaluate(
                        "() => ['user', 'pass'].map((id) => document.getElementById(id).getBoundingClientRect().top"
                        " - document.getElementById(id).closest('label').getBoundingClientRect().top)"
                    )
                finally:
                    browser.close()
        self.assertAlmostEqual(tops[0], tops[1], delta=0.5)


if __name__ == "__main__":
    unittest.main()
