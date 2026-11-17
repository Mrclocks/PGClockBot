"""REAL browser E2E for panel-wide layout rules, against the shipped stylesheets.

- A stack of boxes (a page stack, a settings stack) spaces its boxes by its gap only: 16 apart, never touching.
- A Latin label in a capsule is trimmed to its cap height so the capsule centres its letters; Persian is not.
- A custom select sits where a plain field does, so fields side by side line up.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "list-inputs.css", "panel-nav-modes.css", "panel-ds.css")
PAGE = """<!doctype html><html lang="fa" dir="rtl" data-theme="dark"><head><meta charset="utf-8" />{links}</head>
<body><main class="main ds" id="main"><div class="main-body">
<div class="user-edit-page ds-page-stack">
  <section class="edit-section" id="s1"><h3>یک</h3></section>
  <section class="edit-section" id="s2"><h3>دو</h3></section>
</div>
<div class="msg-vars-stack">
  <section class="card card-flush" id="c1"><h2>الف</h2></section>
  <section class="card card-flush" id="c2"><h2>ب</h2></section>
</div>
<div class="settings-grid">
  <label class="form-field">سقف<input id="plain" /></label>
  <label class="form-field">روش<div class="ui-select" id="custom"><button type="button" class="ui-select-toggle">فوری</button></div></label>
</div>
<label class="ui-switch-chip"><span class="ui-switch-copy"><strong dir="auto" id="latin">VLESS TCP 443</strong></span></label>
<label class="ui-switch-chip"><span class="ui-switch-copy"><strong dir="auto" id="persian">پیش‌فرض</strong></span></label>
</div></main></body></html>"""


class UiConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
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
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(page_file.as_uri())
                    cls.found = page.evaluate(
                        """() => {
                          const r = (id) => document.getElementById(id).getBoundingClientRect();
                          const trim = (id) => getComputedStyle(document.getElementById(id)).textBoxTrim;
                          return {
                            pageGap: r('s2').top - r('s1').bottom,
                            stackGap: r('c2').top - r('c1').bottom,
                            fieldTops: [r('plain').top, r('custom').top],
                            latinTrim: trim('latin'), persianTrim: trim('persian'),
                          };
                        }"""
                    )
                finally:
                    browser.close()

    def test_boxes_in_a_page_stack_are_16_apart(self) -> None:
        self.assertAlmostEqual(self.found["pageGap"], 16, delta=0.5)

    def test_boxes_in_a_settings_stack_are_16_apart(self) -> None:
        self.assertAlmostEqual(self.found["stackGap"], 16, delta=0.5)

    def test_a_custom_select_lines_up_with_a_plain_field(self) -> None:
        plain, custom = self.found["fieldTops"]
        self.assertAlmostEqual(plain, custom, delta=0.5)

    def test_only_a_latin_label_is_trimmed_to_its_letters(self) -> None:
        self.assertEqual(self.found["latinTrim"], "trim-both")
        self.assertIn(self.found["persianTrim"], ("none", ""))


if __name__ == "__main__":
    unittest.main()
