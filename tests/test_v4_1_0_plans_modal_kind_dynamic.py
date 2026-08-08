"""v4.1.0 — plans modal: dynamic kind options by audience (ui-select rebuild)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PlansModalDynamicKindTests(unittest.TestCase):
    def test_kind_select_marked_dynamic(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn('id="plan-kind"', html)
        self.assertIn("data-plan-kind-dynamic", html)
        self.assertIn("data-ui-select-watch", html)
        # Static user-only kinds must NOT be hard-coded as permanent HTML options
        # (JS fills from USER_KINDS / RESELLER_KINDS). Placeholder may have fixed only.
        kind_block = html[
            html.find('id="plan-kind"') : html.find('id="plan-kind"') + 400
        ]
        self.assertNotIn('value="custom"', kind_block)
        self.assertNotIn('value="trial"', kind_block)
        self.assertNotIn('value="wholesale"', kind_block)

    def test_js_has_separate_kind_lists(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("USER_KINDS", html)
        self.assertIn("RESELLER_KINDS", html)
        self.assertIn("function setKindOptions", html)
        self.assertIn("function kindsFor", html)
        # Reseller kinds only fixed + payg
        reseller_block = html[
            html.find("const RESELLER_KINDS") : html.find("const RESELLER_KINDS") + 280
        ]
        self.assertIn("payg", reseller_block)
        self.assertIn("fixed", reseller_block)
        self.assertNotIn("wholesale", reseller_block)
        self.assertNotIn("custom", reseller_block)
        self.assertNotIn("trial", reseller_block)

    def test_audience_change_resets_kind(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("Switching audience: reset to the first kind", html)
        self.assertIn("kindsFor(audience)[0].value", html)
        self.assertIn("syncResellerBilling", html)

    def test_panel_js_watches_all_select_option_mutations(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        # Must rebuild ui-select for every enhanced select, not only settings forms
        self.assertIn("Always rebuild custom menu when <option> list is rewritten", js)
        idx = js.find("function enhanceSelect")
        block = js[idx : idx + 9000]
        self.assertIn("MutationObserver", block)
        # Old gated watch must be gone
        self.assertNotIn(
            "sel.closest('.settings-form, form[data-ui-select-watch], [data-ui-select-watch]')",
            block,
        )


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")


if __name__ == "__main__":
    unittest.main()
