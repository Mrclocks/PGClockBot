"""v4.3.9 — ui-select gap + body-port placement (no stuck menu)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")


class UiSelectGapPlacementTests(unittest.TestCase):
    def test_gap_constant_is_8(self):
        self.assertIn("UI_SELECT_GAP = 8", JS)
        place = JS.split("function placeUiSelectMenu")[1].split("function clearUiSelectMenuPos")[0]
        self.assertIn("rect.bottom + gap", place)
        self.assertIn("rect.top - gap - mh", place)

    def test_menu_ported_to_body(self):
        place = JS.split("function placeUiSelectMenu")[1].split("function clearUiSelectMenuPos")[0]
        self.assertIn("document.body.appendChild(menu)", place)
        self.assertIn("is-ported", place)
        self.assertIn("setProperty", place)
        self.assertIn("'important'", place)

    def test_css_no_inset_auto_important(self):
        block = CSS.split(".ui-select-menu.is-fixed-pos")[1].split("Absolute drop-up")[0]
        self.assertIn("inset-inline: auto !important", block)
        self.assertNotIn("inset: auto !important", block)

    def test_css_fallback_gap_8px(self):
        menu = CSS.split(".ui-select-menu {")[1].split("}")[0]
        # Must stay in sync with JS UI_SELECT_GAP = 8 (--space-1)
        self.assertIn("top: calc(100% + var(--space-1))", menu)
        self.assertIn("UI_SELECT_GAP = 8", JS)

    def test_smart_up_down_preserved(self):
        place = JS.split("function placeUiSelectMenu")[1].split("function clearUiSelectMenuPos")[0]
        self.assertIn("ticket-status-form, .ticket-status-actions", place)
        self.assertIn("roomBelow >= mh", place)
        self.assertIn("roomAbove >= mh", place)
        self.assertIn("openUp", place)

    def test_restore_on_close(self):
        self.assertIn("function restoreUiSelectMenu", JS)
        close = JS.split("function closeUiSelects")[1].split("function enhanceSelect")[0]
        self.assertIn("restoreUiSelectMenu", close)
        self.assertIn("_portedMenu", close)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


if __name__ == "__main__":
    unittest.main()
