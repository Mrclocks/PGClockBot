"""v4.3.8 — kill native OS select pickers (iOS label activation)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")


class NativeSelectKillTests(unittest.TestCase):
    def test_native_select_is_display_none(self):
        block = CSS.split(".ui-select-native {")[1].split("}")[0]
        self.assertIn("display: none !important", block)
        self.assertIn("pointer-events: none !important", block)

    def test_enhance_moves_select_out_of_label(self):
        block = JS.split("function enhanceSelect")[1].split("function enhanceAllSelects")[0]
        self.assertIn("hostLabel", block)
        self.assertIn("insertBefore(sel, hostLabel.nextSibling)", block)
        self.assertIn("removeAttribute('for')", block)
        self.assertIn("iOS", block)
        self.assertIn("native pickers", block)

    def test_open_modal_never_focuses_native_select(self):
        block = JS.split("function openModal")[1].split("window.openModal")[0]
        self.assertIn("enhanceAllSelects(el)", block)
        self.assertIn(".ui-select-toggle", block)
        self.assertNotIn("select:not([disabled])", block)
        self.assertIn("ui-select-native", block)

    def test_mutation_observer_enhances_new_selects(self):
        self.assertIn("selectMo.observe", JS)
        self.assertIn("childList: true, subtree: true", JS)

    def test_capture_guards_block_native_activation(self):
        self.assertIn("select.ui-select-native", JS)
        self.assertIn("mousedown", JS)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")


if __name__ == "__main__":
    unittest.main()
