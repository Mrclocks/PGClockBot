"""v4.3.6 — consistent wallet tx title spacing + reseller volume right-align."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ResellerVolumeAlignTests(unittest.TestCase):
    def test_quota_nowrap_markup(self):
        html = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertIn("reseller-quota-line", html)
        self.assertIn("reseller-quota-cell", html)
        self.assertIn("reseller-quota", html)

    def test_quota_css_right_align(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        block = css.split(".reseller-quota {")[1].split("}")[0]
        self.assertIn("align-items: flex-start", block)
        self.assertIn("text-align: right", block)
        self.assertIn("direction: rtl", block)
        self.assertIn("white-space: nowrap", css.split(".reseller-quota-line")[1].split("}")[0])
        self.assertIn("min-width: 7.5rem", css)


class WalletTxSpacingTests(unittest.TestCase):
    def test_wallet_tx_block_gap_matches_section_head(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        block = css.split(".wallet-tx-block {")[1].split("}")[0]
        self.assertIn("gap: var(--space-2)", block)
        self.assertNotIn("gap: var(--space-3)", block)

    def test_templates_use_section_head_inside_block(self):
        for rel in (
            "app/web/templates/_reseller_edit_body.html",
            "app/web/templates/_user_edit_body.html",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("wallet-tx-block", src)
            self.assertIn("edit-section-head", src)
            # Title+caption stay inside head so gap only applies head→list
            payg = src
            if "تراکنش‌های مصرف PAYG" in src:
                chunk = src.split("تراکنش‌های مصرف PAYG")[1].split("wallet-tx-list")[0]
                self.assertIn("wallet-tx-caption", chunk)
                self.assertIn("</header>", chunk)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")


if __name__ == "__main__":
    unittest.main()
