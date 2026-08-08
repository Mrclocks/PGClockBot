"""v4.3.4 — modal select alignment, reseller tx list, quota space."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SelectFixedPosTests(unittest.TestCase):
    def test_js_clears_inset_inline(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("is-fixed-pos", js)
        self.assertIn("inset-inline", js)
        block = js.split("function placeUiSelectMenu")[1].split("function clearUiSelectMenuPos")[0]
        self.assertIn("rect.left", block)
        self.assertIn("rect.width", block)
        self.assertIn("setProperty", block)

    def test_css_fixed_override(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".ui-select-menu.is-fixed-pos", css)
        self.assertIn("inset-inline: auto !important", css)
        # Must NOT use inset:auto !important — that kills JS top/left
        block = css.split(".ui-select-menu.is-fixed-pos")[1].split(".ui-select.drop-up")[0]
        self.assertNotIn("inset: auto !important", block)


class ResellerTxListTests(unittest.TestCase):
    def test_reseller_uses_wallet_tx_list(self):
        src = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("wallet-tx-list", src)
        self.assertIn("is-debit", src)
        self.assertIn("is-credit", src)
        self.assertIn("تراکنش‌های مصرف PAYG", src)
        # No legacy wide tx tables in PAYG section
        payg = src.split("مدیریت PAYG")[1].split("تغییر نقش")[0]
        self.assertNotIn("<table", payg)


class QuotaMobileTests(unittest.TestCase):
    def test_pg_quota_stays_two_col_on_mobile(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        idx = css.find("/* PG users/traffic: keep side-by-side")
        self.assertGreater(idx, 0)
        block = css[idx : idx + 800]
        self.assertIn(".pg-quota-gauges", block)
        self.assertIn("grid-template-columns: 1fr 1fr", block)
        self.assertIn("height: auto", block)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")


if __name__ == "__main__":
    unittest.main()
