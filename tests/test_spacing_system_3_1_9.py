"""Unified spacing system — design tokens on a 4px grid.

Scale:
  --space-0: 4px
  --space-1: 8px
  --space-1_5: 12px
  --space-2: 16px
  --space-2_5: 20px
  --space-3: 24px
  --space-4: 32px
  --space-4_5: 40px
  --space-5: 48px
  --space-6: 64px
  --card-pad → var(--space-2)
  --section-gap / --page-title-gap → var(--space-3)

Spacing props (margin/padding/gap) must not use off-scale raw px.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
TEMPLATES = ROOT / "app/web/templates"
JS = ROOT / "app/web/static/panel.js"

PROP_RE = re.compile(
    r"(?<![\w-])(?P<prop>(?:margin|padding|gap|row-gap|column-gap)(?:-[a-z]+)?)\s*:\s*(?P<val>[^;]+);"
)
STYLE_RE = re.compile(r'style="([^"]*)"', re.I)
INLINE_SPACE_RE = re.compile(
    r"(?<![\w-])((?:margin|padding|gap|row-gap|column-gap)(?:-[a-z]+)?)\s*:\s*([^;\"]+)",
    re.I,
)

# Raw px only allowed if on the spacing scale (prefer tokens; 0 is fine)
ALLOWED_RAW_PX = {4, 8, 12, 16, 20, 24, 32, 40, 48, 64}
TOKEN_DEFS = (
    "--space-0: 4px;",
    "--space-1: 8px;",
    "--space-1_5: 12px;",
    "--space-2: 16px;",
    "--space-2_5: 20px;",
    "--space-3: 24px;",
    "--space-4: 32px;",
    "--space-4_5: 40px;",
    "--space-5: 48px;",
    "--space-6: 64px;",
    "--card-pad: var(--space-2);",
    "--section-gap: var(--space-3);",
    "--page-title-gap: var(--space-3);",
)

SPACE_TOKEN_NAMES = {
    "--space-0:",
    "--space-1:",
    "--space-1_5:",
    "--space-2:",
    "--space-2_5:",
    "--space-3:",
    "--space-4:",
    "--space-4_5:",
    "--space-5:",
    "--space-6:",
    "--card-pad:",
    "--section-gap:",
    "--page-title-gap:",
    "--control-gap:",
    "--stack-gap:",
}


def _raw_px_in_spacing(css: str) -> list[tuple[str, str, int]]:
    bad: list[tuple[str, str, int]] = []
    for m in PROP_RE.finditer(css):
        prop, val = m.group("prop"), m.group("val")
        for n in re.findall(r"(\d+)px", val):
            num = int(n)
            if num in ALLOWED_RAW_PX or num == 0:
                continue
            bad.append((prop, val.strip(), num))
    return bad


class SpacingTokenDefsTests(unittest.TestCase):
    def test_scale_tokens_defined(self):
        css = CSS.read_text(encoding="utf-8")
        for line in TOKEN_DEFS:
            self.assertIn(line, css)

    def test_no_unexpected_spacing_token_names(self):
        css = CSS.read_text(encoding="utf-8")
        root = css.split(":root {", 1)[1].split("}", 1)[0]
        space_vars = re.findall(
            r"--(?:space-[\d_]+|card-pad|section-gap|page-title-gap|control-gap|stack-gap)\s*:",
            root,
        )
        for name in space_vars:
            self.assertIn(name, SPACE_TOKEN_NAMES, msg=f"unexpected spacing token {name}")

    def test_scale_token_count(self):
        css = CSS.read_text(encoding="utf-8")
        root = css.split(":root {", 1)[1].split("}", 1)[0]
        for name in SPACE_TOKEN_NAMES:
            self.assertIn(name, root)


class SpacingLiteralBanTests(unittest.TestCase):
    def test_css_spacing_props_use_tokens_only(self):
        css = CSS.read_text(encoding="utf-8")
        bad = _raw_px_in_spacing(css)
        # Allow calc(... * -1) proximity pulls that reference space tokens
        bad = [
            b
            for b in bad
            if not re.search(
                r"calc\(\s*(?:-1\s*\*\s*)?var\(--space-[\d_]+\)(?:\s*\*\s*-1)?\s*\)",
                b[1],
            )
            and not re.search(
                r"calc\(\s*var\(--space-[\d_]+\)\s*\*\s*-1\s*\)",
                b[1],
            )
        ]
        self.assertEqual(
            bad,
            [],
            msg=f"off-scale or untokenized spacing px remain: {bad[:20]}",
        )

    def test_templates_have_no_raw_spacing_px(self):
        offenders: list[str] = []
        for path in TEMPLATES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            # Skip miniapp self-contained style block token defs (values are the scale)
            if path.name == "miniapp.html":
                # Only check style="" attributes in miniapp, not the <style> token defs
                for sm in STYLE_RE.finditer(text):
                    for m in INLINE_SPACE_RE.finditer(sm.group(1)):
                        val = m.group(2)
                        if re.search(r"\d+px|\d+(?:\.\d+)?rem", val):
                            offenders.append(f"{path.name}: {m.group(1)}: {val.strip()}")
                continue
            for sm in STYLE_RE.finditer(text):
                for m in INLINE_SPACE_RE.finditer(sm.group(1)):
                    val = m.group(2)
                    if re.search(r"\d+px|\d+(?:\.\d+)?rem", val):
                        offenders.append(f"{path.name}: {m.group(1)}: {val.strip()}")
        self.assertEqual(offenders, [], msg="inline spacing must use tokens")

    def test_menu_port_gap_matches_space_1(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("const gap = 8", js)


class SpacingSemanticParityTests(unittest.TestCase):
    def test_page_title_gaps_use_token(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-4) 0;",
            css,
        )
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-2) 0;",
            css,
        )
        foot = css.split(".site-footer {\n", 1)[1].split("}", 1)[0]
        self.assertIn("padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));", foot)
        head = css.split(".page-head {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-bottom: var(--page-title-gap);", head)

    def test_cards_and_sections_share_tokens(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".card {\n  padding: var(--card-pad);\n  margin-bottom: var(--section-gap);", css)
        self.assertIn("gap: var(--section-gap);", css)
        self.assertIn("margin-bottom: var(--section-gap);", css)
        self.assertIn("--section-gap: var(--space-3);", css)
        self.assertIn("--card-pad: var(--space-2);", css)

    def test_form_label_input_and_help_use_scale(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("input, select, textarea {\n  width: 100%;\n  margin-top: var(--space-1);", css)
        help_block = re.search(
            r"(?ms)label > small\.muted,\s*\.hint\s*\{([^}]+)\}",
            css,
        )
        self.assertIsNotNone(help_block)
        # Help sits close to its control (Law of Proximity)
        self.assertIn("margin-top: var(--space-1)", help_block.group(1))

    def test_title_caption_proximity(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".card h2 + p.muted,", css)
        self.assertIn("margin-top: calc(-1 * var(--space-1));", css)

    def test_flush_table_head_utility(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".card.card-flush > .card-flush-head {", css)
        self.assertIn(".form-field-label {", css)


if __name__ == "__main__":
    unittest.main()
