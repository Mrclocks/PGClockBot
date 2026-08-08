"""Pinned column footers — content scrolls, footers stay baseline-aligned."""

from __future__ import annotations

import unittest
from pathlib import Path


CSS = Path("app/web/static/panel.css")
BASE = Path("app/web/templates/base.html")


class FooterPinnedShellTests(unittest.TestCase):
    def test_no_main_shell_wrapper(self):
        html = BASE.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("main-shell", html)
        self.assertNotIn(".main-shell", css)
        main = html.split('<main class="main">', 1)[1].split("</main>", 1)[0]
        self.assertIn('class="main-body"', main)
        self.assertIn('class="site-footer"', main)
        self.assertGreater(main.find("site-footer"), main.find("main-body"))

    def test_document_scroll_locked_to_shell(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("html:has(.shell)", css)
        lock = css.split("html:has(.shell)", 1)[1].split(".shell {", 1)[0]
        self.assertIn("overflow: hidden;", lock)
        self.assertIn("100svh", lock)

    def test_main_does_not_scroll_as_a_column(self):
        css = CSS.read_text(encoding="utf-8")
        main = css.split(".main {\n", 1)[1].split(".main-body {", 1)[0]
        self.assertIn("overflow: hidden;", main)
        self.assertNotIn("overflow-y: auto;", main)
        self.assertIn("padding: var(--page-title-gap) var(--space-4) 0;", main)

    def test_main_body_is_the_scrollport(self):
        css = CSS.read_text(encoding="utf-8")
        body = css.split(".main-body {\n", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 1 1 auto;", body)
        self.assertIn("min-height: 0;", body)
        self.assertIn("overflow-y: auto;", body)
        self.assertIn("padding-bottom: var(--page-title-gap);", body)

    def test_side_nav_scrolls_footer_pinned(self):
        css = CSS.read_text(encoding="utf-8")
        side = css.split(".side {\n", 1)[1].split(".main {", 1)[0]
        self.assertIn("overflow: hidden;", side)
        self.assertIn("padding: calc(var(--space-2) + var(--safe-top)) var(--space-2) 0;", side)
        nav = css.split(".side-nav {\n", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y: auto;", nav)
        self.assertIn("min-height: 0;", nav)

    def test_site_footer_pinned_not_margin_auto_scroll_hack(self):
        css = CSS.read_text(encoding="utf-8")
        foot = css.split(".site-footer {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-top: 0;", foot)
        self.assertIn("flex: 0 0 auto;", foot)
        self.assertIn("padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));", foot)
        self.assertNotIn("position: fixed", foot)
        self.assertNotIn("position: sticky", foot)
        side = css.split(".side-foot {\n", 1)[1].split("}", 1)[0]
        self.assertIn("padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));", side)
        shared = "min-height: calc(var(--page-title-gap) + 28px + var(--page-title-gap) + var(--safe-bottom));"
        self.assertIn(shared, foot)
        self.assertIn(shared, side)


if __name__ == "__main__":
    unittest.main()
