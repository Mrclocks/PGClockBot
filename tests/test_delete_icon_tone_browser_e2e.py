"""REAL browser E2E: a delete icon is always red, and a list value's edit and delete are bare icons.

Inside the list editor («مقدار جدید»), each value's pencil and trash sat in their own circles inside the value's
capsule. They are now bare icons, the trash red and the pencil muted, in both themes. A trash button elsewhere
(`.ds-del`) was grey until hovered; it is red too.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "panel-ds.css", "list-inputs.css")
PAGE = """<!doctype html><html lang="fa" dir="rtl" data-theme="{theme}"><head><meta charset="utf-8" />{links}</head>
<body><main class="main ds"><div class="form-field list-input-field"><label for="ids">آیدی ادمین‌ها</label>
<input id="ids" name="ids" value="6127326933" data-list-editor="telegram" data-normalize-digits="0" dir="ltr" /></div>
<button type="button" class="ds-del" id="del" aria-label="حذف"><svg viewBox="0 0 24 24"><path d="M4 7h16"/></svg></button>
</main></body></html>"""
PROBE = """() => {
  document.querySelector('.list-editor-toggle').click();
  const ctx = document.createElement('canvas').getContext('2d');
  const rgb = (c) => { ctx.fillStyle = '#000'; ctx.fillStyle = c; return ctx.fillStyle; };
  const root = getComputedStyle(document.documentElement);
  const look = (el) => { const cs = getComputedStyle(el);
    return { color: rgb(cs.color), bg: cs.backgroundColor, edge: cs.borderTopColor }; };
  const row = document.querySelector('.list-editor-row');
  return {
    edit: look(row.querySelector('[data-list-action="edit"]')),
    remove: look(row.querySelector('[data-list-action="delete"]')),
    add: look(document.querySelector('.list-editor-entry .icon-btn')),
    dsDel: look(document.getElementById('del')),
    red: rgb(root.getPropertyValue('--destructive-fg').trim()),
  };
}"""
CLEAR = "rgba(0, 0, 0, 0)"


class DeleteIconToneTests(unittest.TestCase):
    def _probe(self, theme: str) -> dict:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        links = "".join(f'<link rel="stylesheet" href="{(STATIC / name).as_uri()}" />' for name in SHEETS)
        with tempfile.TemporaryDirectory() as tmp:
            page_file = Path(tmp) / "page.html"
            page_file.write_text(PAGE.format(theme=theme, links=links), encoding="utf-8")
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                try:
                    page = browser.new_page()
                    page.goto(page_file.as_uri())
                    # panel.js supplies the digit normaliser on real pages; the styling under test does not need it.
                    page.add_script_tag(content="window.normalizePanelNumberText = (text) => String(text);")
                    for script in ("money-inputs.js", "list-inputs.js"):
                        page.add_script_tag(path=str(STATIC / script))
                    return page.evaluate(PROBE)
                finally:
                    browser.close()

    def test_a_values_edit_and_delete_have_no_circle(self) -> None:
        for theme in ("dark", "light"):
            got = self._probe(theme)
            for action in ("edit", "remove"):
                self.assertEqual(got[action]["bg"], CLEAR, f"{theme} {action}: no fill")
                self.assertEqual(got[action]["edge"], CLEAR, f"{theme} {action}: no ring")
            self.assertNotEqual(got["add"]["bg"], CLEAR, f"{theme}: the «+» beside the field keeps its circle")

    def test_every_trash_icon_is_red(self) -> None:
        for theme in ("dark", "light"):
            got = self._probe(theme)
            self.assertEqual(got["remove"]["color"], got["red"], f"{theme}: list value trash")
            self.assertEqual(got["dsDel"]["color"], got["red"], f"{theme}: trash button")
            self.assertNotEqual(got["edit"]["color"], got["red"], f"{theme}: the pencil is not red")


if __name__ == "__main__":
    unittest.main()
