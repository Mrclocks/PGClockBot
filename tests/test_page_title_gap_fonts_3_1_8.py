"""Equal page-title gaps + single font family (3.1.8).

Above title (.main padding-top) == below title (.page-head margin-bottom).
Bot and PasarGuard use the same --page-title-gap token.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


CSS = Path("app/web/static/panel.css")
BASE = Path("app/web/templates/base.html")


class PageTitleGapParityTests(unittest.TestCase):
    def test_page_title_gap_token(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("--page-title-gap: var(--space-3);", css)

    def test_main_top_matches_page_head_bottom(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-4) 0;",
            css,
        )
        self.assertIn(".page-head {\n  display: flex;", css)
        head = css.split(".page-head {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-bottom: var(--page-title-gap);", head)
        # no asymmetric title→tabs shrink
        self.assertNotIn(".page-head:has(+ .section-tabs)", css)
        self.assertNotIn(".page-head:has(+ .settings-tabs)", css)

    def test_pg_title_gap_identical_to_bot(self):
        css = CSS.read_text(encoding="utf-8")
        # PG pages use the same .page-head gap token as bot (no separate stack)
        head = css.split(".page-head {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-bottom: var(--page-title-gap);", head)
        mobile = css.split("@media (max-width: 900px)", 1)[1]
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-2) 0;",
            mobile,
        )
        self.assertIn(".page-head { margin-bottom: var(--page-title-gap);", mobile)

    def test_pg_pages_use_plain_page_head_like_bot(self):
        pages = sorted(Path("app/web/templates").glob("pg_*.html"))
        self.assertTrue(pages)
        for path in pages:
            src = path.read_text(encoding="utf-8")
            self.assertIn('class="page-head"', src, msg=path.name)
            self.assertNotIn('class="pg-head"', src, msg=path.name)
            self.assertNotIn("pg_tabs(", src, msg=path.name)


class PanelFontUnityTests(unittest.TestCase):
    def test_single_font_family_token(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn('--font: "Vazirmatn", sans-serif;', css)
        self.assertNotIn("--mono:", css)
        self.assertNotIn("Cascadia", css)
        self.assertNotIn("Consolas", css)
        self.assertNotIn("Tahoma", css)
        self.assertNotIn("ui-monospace", css)
        self.assertNotIn("ui-sans-serif", css)
        self.assertNotIn("system-ui", css)
        # mono utility keeps numerals tabular but same family
        self.assertIn(".mono { font-family: var(--font);", css)
        self.assertIn('font-family: var(--font);', css)
        # no leftover mono variable references
        self.assertNotIn("var(--mono)", css)

    def test_base_loads_vazirmatn_only(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertIn("family=Vazirmatn", html)
        self.assertEqual(html.count("fonts.googleapis.com/css2"), 1)


if __name__ == "__main__":
    unittest.main()
