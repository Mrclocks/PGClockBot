"""REAL browser E2E: a button inside a coloured notice is the notice's colour, text and edge alike.

A main-action button is drawn in the panel brand colour on a light fill. Inside a yellow warning the fill
blended into the notice, so the button looked yellow while its text stayed brand orange. The notice now
hands its colour to its buttons; red and ghost buttons keep their own colour, and buttons outside a
notice keep the brand colour (also with another brand preset).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "panel-ds.css")
PAGE = """<!doctype html><html lang="fa" dir="rtl" data-theme="{theme}"{brand}><head><meta charset="utf-8" />{links}</head>
<body><main class="main ds" id="main">
<div class="flash warn" id="warn"><a class="btn btn-sm" id="warn-btn" href="#">مشاهده</a>
  <a class="btn btn-sm btn-ok" id="warn-ok" href="#">آپدیت</a><button class="btn btn-sm btn-warn" id="warn-warn">هشدار</button>
  <button class="btn btn-sm btn-danger" id="warn-danger">حذف</button><button class="btn btn-sm btn-ghost" id="warn-ghost">بستن</button></div>
<div class="flash err" id="err"><a class="btn btn-sm" id="err-btn" href="#">تنظیم</a></div>
<div class="flash ok" id="ok"><a class="btn btn-sm" id="ok-btn" href="#">ادامه</a></div>
<a class="btn" id="plain" href="#">ذخیره</a><button class="btn btn-danger" id="plain-danger">حذف</button>
</main></body></html>"""

PROBE = """() => {
  const ctx = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  const rgb = (c) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1);
    return Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3)).join(','); };
  const out = {};
  document.querySelectorAll('[id]').forEach((el) => {
    const cs = getComputedStyle(el);
    // Border colours are drawn opaque here so only the hue is compared, not the 72% mix.
    const solid = (c) => rgb(c.replace(/\\/\\s*[\\d.]+\\s*\\)$/, ')').replace(/,\\s*[\\d.]+\\)$/, ')'));
    out[el.id] = { text: rgb(cs.color), edge: solid(cs.borderTopColor) };
  });
  out.vars = { warn: rgb(getComputedStyle(document.documentElement).getPropertyValue('--warn')),
    danger: rgb(getComputedStyle(document.documentElement).getPropertyValue('--destructive')),
    ok: rgb(getComputedStyle(document.documentElement).getPropertyValue('--ok')),
    brand: rgb(getComputedStyle(document.documentElement).getPropertyValue('--brand')) };
  return out;
}"""


class NoticeButtonToneTests(unittest.TestCase):
    def _colours(self, theme: str, brand: str = "") -> dict[str, dict[str, str]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        links = "".join(f'<link rel="stylesheet" href="{(STATIC / name).as_uri()}" />' for name in SHEETS)
        brand_attr = f' data-brand="{brand}"' if brand else ""
        with tempfile.TemporaryDirectory() as tmp:
            page_file = Path(tmp) / "page.html"
            page_file.write_text(PAGE.format(theme=theme, brand=brand_attr, links=links), encoding="utf-8")
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                try:
                    page = browser.new_page()
                    page.goto(page_file.as_uri())
                    return page.evaluate(PROBE)
                finally:
                    browser.close()

    def assert_toned(self, colours: dict, button: str, notice: str, tone: str) -> None:
        self.assertEqual(colours[button]["text"], colours[notice]["text"], f"{button}: text follows the notice")
        self.assertEqual(colours[button]["edge"], colours["vars"][tone], f"{button}: edge follows the notice")

    def test_buttons_in_a_yellow_warning_are_yellow_text_and_edge(self) -> None:
        for theme in ("dark", "light"):
            colours = self._colours(theme)
            for button in ("warn-btn", "warn-ok", "warn-warn"):
                self.assert_toned(colours, button, "warn", "warn")

    def test_red_and_green_notices_colour_their_buttons_too(self) -> None:
        colours = self._colours("dark")
        self.assert_toned(colours, "err-btn", "err", "danger")
        self.assert_toned(colours, "ok-btn", "ok", "ok")

    def test_red_and_ghost_buttons_keep_their_own_colour_inside_a_notice(self) -> None:
        colours = self._colours("dark")
        self.assertEqual(colours["warn-danger"]["text"], colours["plain-danger"]["text"])
        self.assertNotEqual(colours["warn-ghost"]["text"], colours["warn"]["text"])

    def test_buttons_outside_a_notice_keep_the_brand_colour(self) -> None:
        for brand in ("", "purple"):
            colours = self._colours("dark", brand)
            self.assertEqual(colours["plain"]["edge"], colours["vars"]["brand"], brand or "orange")
            self.assertNotEqual(colours["plain"]["text"], colours["warn"]["text"])


if __name__ == "__main__":
    unittest.main()
