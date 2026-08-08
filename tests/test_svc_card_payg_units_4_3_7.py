"""v4.3.7 — svc card redesign, reseller role UI, Persian volume units."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.formatting import format_bytes, format_bytes_ratio

ROOT = Path(__file__).resolve().parents[1]
GB = 1024**3


class SvcCardMetaTests(unittest.TestCase):
    def test_user_edit_uses_meta_stack(self):
        html = (ROOT / "app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("user-edit-meta", html)
        self.assertIn("svc-stat-row", html)
        self.assertIn("volume_text", html)
        self.assertIn("num-ratio", html)
        self.assertNotIn("svc-card-stats", html)
        self.assertNotIn("}GB", html)
        self.assertNotIn(" GB", html)

    def test_css_right_align_meta(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".svc-stat-pill", css)
        pill = css.split(".svc-stat-pill {")[1].split("}")[0]
        self.assertIn("text-align: right", pill)
        self.assertIn(".num-ratio", css)
        ratio = css.split(".num-ratio,\n.byte-size {")[1].split("}")[0]
        self.assertIn("text-align: right", ratio)
        self.assertIn("direction: rtl", ratio)


class ResellerModalLayoutTests(unittest.TestCase):
    def test_role_section_is_first(self):
        src = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(
            encoding="utf-8"
        )
        role_i = src.find("تغییر نقش کاربر")
        edit_i = src.find('action="/resellers/{{ user.id }}/edit"')
        payg_i = src.find("مدیریت PAYG")
        self.assertGreater(role_i, 0)
        self.assertGreater(edit_i, role_i)
        self.assertIn('class="select-block"', src)
        self.assertIn("user-edit-inline-row", src)
        # PAYG status lives in section head, not loose inline margins before save
        self.assertIn("آخرین مصرف صورتحساب‌شده", src)
        self.assertNotIn("watermark", src.lower().split("آخرین مصرف")[0][-200:])
        head = src.split("مدیریت PAYG")[1].split("موجودی کیف پول")[0]
        self.assertIn("وضعیت:", head)
        self.assertNotIn('style="margin-top:', head)
        if payg_i > 0:
            self.assertGreater(payg_i, edit_i)

    def test_payg_rate_uses_gig(self):
        src = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("/ گیگ", src)
        self.assertNotIn("/ GB", src)
        self.assertIn("payg-meta", src)


class VolumeUnitFaTests(unittest.TestCase):
    def test_ratio_unit_once(self):
        text = format_bytes_ratio(10 * GB, 100 * GB)
        self.assertEqual(text, "10 / 100 گیگ")
        self.assertEqual(text.count("گیگ"), 1)

    def test_ratio_az_joiner(self):
        text = format_bytes_ratio(10 * GB, 100 * GB, joiner=" از ")
        self.assertEqual(text, "10 از 100 گیگ")
        self.assertEqual(text.count("گیگ"), 1)

    def test_bot_and_web_no_english_gb_labels(self):
        paths = [
            "app/bot/keyboards.py",
            "app/bot/handlers/reseller.py",
            "app/bot/handlers/admin_pg_users.py",
            "app/services/resellers.py",
            "app/web/templates/plans.html",
            "app/web/templates/miniapp.html",
            "app/web/templates/pg_admins.html",
        ]
        for rel in paths:
            src = (ROOT / rel).read_text(encoding="utf-8")
            # Allow code identifiers like price_per_gb / data_limit_gb
            for line in src.splitlines():
                if "price_per_gb" in line or "data_limit_gb" in line or "extra_gb" in line:
                    continue
                if "GB =" in line or "* GB" in line or "/ GB" in line.replace("/ گیگ", ""):
                    continue
                self.assertNotRegex(
                    line,
                    r"(?<![A-Za-z_/])GB(?![A-Za-z_])",
                    msg=f"English GB label in {rel}: {line.strip()}",
                )


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")


if __name__ == "__main__":
    unittest.main()
