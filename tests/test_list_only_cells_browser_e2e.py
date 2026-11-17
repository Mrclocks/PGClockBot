"""REAL browser E2E: list-only pieces of a phone list stay out of the desktop table.

The users table puts a person's role and risk tag in the service cell for the phone list only. Inside the
``.ds`` scope a badge sets its own display at the same weight as the hide rule, so on desktop they showed up
again in the service column and pushed the services menu off the row's centre.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
SHEETS = ("panel.css", "panel-ds.css", "panel-nav-modes.css")
PAGE = """<!doctype html><html lang="fa" dir="rtl" data-theme="light"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />{links}</head>
<body><main class="main ds" id="main"><table class="table-list-sm"><tbody><tr>
<td class="col-name li-title">نام</td>
<td class="col-svc li-sub"><span class="li-only badge role-admin" id="role">مدیر</span>
<span class="li-only badge badge-risk-tag" id="risk">ریسک بالا</span><span class="li-hide" id="svc">سرویس</span></td>
</tr></tbody></table></main></body></html>"""


class ListOnlyCellsTests(unittest.TestCase):
    def _displays(self, width: int) -> dict[str, str]:
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
                    page = browser.new_page(viewport={"width": width, "height": 800})
                    page.goto(page_file.as_uri())
                    return page.evaluate(
                        "() => Object.fromEntries(['role', 'risk', 'svc'].map("
                        "(id) => [id, getComputedStyle(document.getElementById(id)).display]))"
                    )
                finally:
                    browser.close()

    def test_the_desktop_table_hides_list_only_badges(self) -> None:
        shown = self._displays(1280)
        self.assertEqual(shown["role"], "none")
        self.assertEqual(shown["risk"], "none")
        self.assertNotEqual(shown["svc"], "none")

    def test_the_phone_list_still_shows_them(self) -> None:
        shown = self._displays(390)
        self.assertNotEqual(shown["role"], "none")
        self.assertNotEqual(shown["risk"], "none")


if __name__ == "__main__":
    unittest.main()
