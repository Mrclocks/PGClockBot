"""Loyalty top-referrers column width + modal scroll lock."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
JS = ROOT / "app/web/static/panel.js"
LOYALTY = ROOT / "app/web/templates/loyalty.html"


class LoyaltyTopReferrersColumns(unittest.TestCase):
    def test_invite_count_uses_col_count(self):
        html = LOYALTY.read_text(encoding="utf-8")
        self.assertIn('class="col-count"', html)
        self.assertIn('class="mono col-count"', html)
        self.assertIn("برترین معرف‌ها", html)
        self.assertIn("loyalty-referrers", html)

    def test_col_count_css_shrinks_to_content(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".col-count {", css)
        block = css.split(".col-count {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 50%;", block)
        # 50% first-column is loyalty-only — never all compact tables
        self.assertNotIn(
            ".table-compact th:not(.col-count):first-child,\n.table-compact td:not(.col-count):first-child {",
            css,
        )
        self.assertIn(".loyalty-referrers .table-compact th:not(.col-count):first-child", css)


class ModalScrollLockTests(unittest.TestCase):
    def test_locks_main_scroll_containers(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("body.modal-open .main,", css)
        self.assertIn("body.modal-open .side {", css)
        self.assertIn("overflow: hidden !important;", css)
        self.assertIn("overscroll-behavior: contain;", css)

    def test_settings_modal_panel_scrolls_like_others(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".settings-modal-panel {", css)
        block = css.split(".settings-modal-panel {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: hidden;", block)
        self.assertIn("overscroll-behavior: contain;", block)
        self.assertNotIn("overflow-y: auto;", block)
        body = css.split(".settings-modal-body {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: visible;", body)

    def test_js_lock_helpers_exist(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function lockPageScroll", js)
        self.assertIn("function unlockPageScroll", js)
        self.assertIn("installModalScrollGuards", js)
        # <html> is never flagged/locked — that froze iOS Safari's toolbar.
        self.assertNotIn("documentElement.classList.add('modal-open')", js)
        self.assertIn("document.body.classList.add('modal-open')", js)
        self.assertIn("Always-on guards", js)
        self.assertIn("Prefer the inner scroll shell", js)
        self.assertIn("isModalInteriorScroller", js)


if __name__ == "__main__":
    unittest.main()
