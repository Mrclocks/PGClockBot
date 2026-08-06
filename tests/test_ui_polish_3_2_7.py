"""UI polish 3.2.7 — table chrome, actions overlap, force-join, reseller URL hints."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
JS = ROOT / "app/web/static/panel.js"
RESELLERS = ROOT / "app/web/templates/resellers.html"
SETTINGS_FIELD = ROOT / "app/web/templates/_settings_field.html"


class TableChromeTests(unittest.TestCase):
    def test_thead_matches_panel_title_size(self):
        css = CSS.read_text(encoding="utf-8")
        th = re.search(r"(?ms)^th\s*\{([^}]+)\}", css)
        self.assertIsNotNone(th)
        self.assertIn("font-size: var(--title-size)", th.group(1))
        self.assertIn("font-weight: 600", th.group(1))

    def test_table_wrap_clips_to_radius(self):
        css = CSS.read_text(encoding="utf-8")
        wrap = css.split(".table-wrap {\n", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: auto;", wrap)
        self.assertIn("border-radius: var(--radius);", wrap)
        self.assertIn("isolation: isolate;", wrap)
        self.assertNotIn("scrollbar-gutter: stable;", wrap)
        # Card tables stay radius-clipped; framing comes from surface contrast (no border)
        card = css.split(".card > .table-wrap {\n", 1)[1].split("}", 1)[0]
        self.assertIn("border: none;", card)
        self.assertIn("border-radius: var(--radius);", card)

    def test_row_height_not_over_compact(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(
            ".row-actions-menu .btn-sm,\n.row-actions-menu a.btn.btn-sm,\n.row-actions-menu button.btn-sm {\n  height: 32px;",
            css,
        )
        compact = css.split(".table-compact th,\n.table-compact td {\n", 1)[1].split("}", 1)[0]
        self.assertIn("padding: var(--space-2);", compact)


class ActionsOverlapTests(unittest.TestCase):
    def test_multi_action_rows_force_kebab(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("items.length > 1", js)
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".row-actions-menu > *", css)
        self.assertIn(".row-actions-menu .ui-select", css)


class ForceJoinRedesignTests(unittest.TestCase):
    def test_force_channels_head_and_grid(self):
        html = SETTINGS_FIELD.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        self.assertIn("force-channels-head", html)
        self.assertIn("force-channels-title", html)
        self.assertNotIn("force-channels-footer", html)
        self.assertIn("force-channel-add", js)
        self.assertIn("addTileHtml", js)
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));", css)
        self.assertNotIn("force-channel-fields", js)
        self.assertIn("form-field force-channel-id-wrap", js)


class ResellerUrlHintTests(unittest.TestCase):
    def test_two_line_rtl_captions(self):
        src = RESELLERS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("field-hint-stack", src)
        self.assertIn("در حال استفاده:", src)
        self.assertIn("پیش‌فرض:", src)
        # current address first, default second
        used = src.find("در حال استفاده:")
        default = src.find("پیش‌فرض:", used)
        self.assertGreater(used, 0)
        self.assertGreater(default, used)
        stack = css.split(".field-hint-stack {\n", 1)[1].split("}", 1)[0]
        self.assertIn("text-align: right;", stack)
        self.assertIn("flex-direction: column;", stack)


if __name__ == "__main__":
    unittest.main()
