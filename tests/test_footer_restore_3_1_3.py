"""Footer restored to pre-3.0.4 / 3.0.3 shell layout (3.1.3)."""

from __future__ import annotations

import unittest
from pathlib import Path

from css_blocks import at_rule, declarations, rule


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
BASE = ROOT / "app/web/templates/base.html"


class FooterRestore303Tests(unittest.TestCase):
    def test_no_main_shell_wrapper(self):
        html = BASE.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("main-shell", html)
        self.assertNotIn(".main-shell", css)
        main = html.split('<main class="main">', 1)[1].split("</main>", 1)[0]
        # Allow additional classes appended later (e.g. "page-surface") —
        # what matters is the main-body wrapper class is still first/present.
        self.assertIn('class="main-body', main)
        self.assertIn('class="site-footer"', main)
        self.assertGreater(main.find("site-footer"), main.find("main-body"))

    def test_no_chrome_pad_experiment_tokens(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("--chrome-pad", css)
        self.assertNotIn("--main-pad-", css)
        self.assertNotIn("padding-bottom: var(--chrome-pad-bottom)", css)
        mobile = at_rule(css, "@media (max-width: 900px)")
        self.assertNotIn("--chrome-pad", mobile)
        # Desktop .main keeps --bottom-inset; mobile moves it onto both footers
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-4) var(--bottom-inset);",
            rule(css, ".main"),
        )
        self.assertIn("padding-bottom: 0;", rule(mobile, ".shell"))
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-2) 0;",
            rule(mobile, ".main"),
        )
        self.assertIn(
            "padding-bottom: var(--bottom-inset);", rule(mobile, ".site-footer")
        )

    def test_side_scrolls_as_a_column(self):
        css = CSS.read_text(encoding="utf-8")
        side = rule(css, ".side")
        self.assertIn("overflow-y: auto;", side)
        # .shell is the single owner of the notch insets on desktop/tablet, so
        # .side must not add --safe-top a second time.
        self.assertIn(
            "padding: var(--space-2) var(--space-2) var(--bottom-inset);",
            side,
        )
        self.assertNotIn("--safe-top", declarations(side))
        self.assertIn("padding-top: var(--safe-top);", rule(css, ".shell"))
        self.assertNotIn("padding-bottom: var(--chrome-pad-bottom)", css)

    def test_site_footer_classic_sticky(self):
        css = CSS.read_text(encoding="utf-8")
        foot = rule(css, ".site-footer")
        self.assertIn("margin-top: auto;", foot)
        self.assertIn("padding-top: var(--page-title-gap);", foot)
        self.assertNotIn("position: fixed", foot)
        self.assertNotIn("position: sticky", foot)


if __name__ == "__main__":
    unittest.main()
