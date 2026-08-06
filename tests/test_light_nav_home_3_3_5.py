"""Light theme: web-panel sidebar box stays border-only (no gray fill)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"


class LightNavHomeBoxTests(unittest.TestCase):
    def _light_block(self) -> str:
        css = CSS.read_text(encoding="utf-8")
        marker = 'html[data-theme="light"] .nav-section-home'
        self.assertIn(marker, css)
        # From home section rule through bot section (covers home active/hover)
        start = css.index(marker)
        end = css.index('html[data-theme="light"] .nav-section-bot', start)
        return css[start:end]

    def test_home_box_not_grouped_with_modal_backdrop(self):
        css = CSS.read_text(encoding="utf-8")
        # Regression: home was accidentally sharing rgba(0,0,0,0.28) with backdrop
        bad = re.search(
            r'html\[data-theme="light"\]\s*\.nav-section-home\s*,\s*'
            r'html\[data-theme="light"\]\s*\.ui-modal-backdrop',
            css,
        )
        self.assertIsNone(bad, "nav-section-home must not share modal backdrop background")

    def test_home_box_is_soft_fill_without_border(self):
        block = self._light_block()
        home = block.split("{", 1)[1].split("}", 1)[0]
        # Soft neutral fill replaces border framing (borderless soft UI)
        self.assertIn("var(--foreground)", home)
        self.assertIn("border: none;", home)
        self.assertNotIn("rgba(0, 0, 0", home)

    def test_home_active_and_hover_are_neutral(self):
        block = self._light_block()
        self.assertIn(".nav-item-home.active", block)
        self.assertIn(".nav-item-home:hover", block)
        active = block.split(".nav-item-home.active", 1)[1].split("}", 1)[0]
        hover = block.split(".nav-item-home:hover", 1)[1].split("}", 1)[0]
        # Neutral tint from foreground — not brand orange/blue
        self.assertIn("var(--foreground)", active)
        self.assertIn("var(--foreground)", hover)
        self.assertNotIn("#ea580c", active + hover)
        self.assertNotIn("#2563eb", active + hover)
        self.assertNotIn("rgba(0, 0, 0, 0.28)", active + hover)


if __name__ == "__main__":
    unittest.main()
