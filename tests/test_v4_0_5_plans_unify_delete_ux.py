"""v4.0.5 — plans unify, delete wallet warn, neutral row-actions."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]


class DeleteWalletWarnTests(unittest.TestCase):
    def test_users_delete_warns_wallet(self):
        html = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("wallet_balance", html)
        self.assertIn("موجودی کیف پول", html)
        self.assertIn("سفارش‌ها", html)
        body = (ROOT / "app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("موجودی کیف پول", body)
        self.assertIn("/users/{{ user.id }}/delete", body)

    def test_reseller_full_delete_warns_wallet(self):
        html = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertIn("wallet_balance", html)
        self.assertIn("موجودی کیف پول", html)
        # PAYG money lives on shop wallet — do not double-warn a separate billing pot
        self.assertNotIn("موجودی PAYG", html)
        # Wallet label lives on the shared edit body (page is a thin include wrapper)
        body = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("موجودی کیف پول", body)
        self.assertNotIn("موجودی PAYG:", body)


class PlansUnifyTests(unittest.TestCase):
    def test_single_add_button_and_unified_modal(self):
        html = template_with_includes("plans.html")
        self.assertIn("افزودن پلن", html)
        self.assertIn("modal-plan-unified", html)
        self.assertIn("plan-audience", html)
        self.assertIn("plan-kind", html)
        self.assertIn("پلن‌های کاربران", html)
        self.assertIn("reseller-plans", html)
        self.assertIn('action="/resellers/plans"', html)
        self.assertNotIn("commission_percent", html)
        self.assertIn("billing_mode", html)
        # Old multi-button create chrome removed
        self.assertNotIn('data-modal-open="modal-plan-create"', html)
        self.assertNotIn('data-modal-open="modal-trial"', html)

    def test_reseller_plans_redirects(self):
        src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def reseller_plans_page") : src.find(
                "async def reseller_plan_create"
            )
        ]
        self.assertIn('RedirectResponse("/plans#reseller-plans"', fn)

    def test_billing_mode_on_model(self):
        src = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        block = src[src.find("class ResellerPlan") : src.find("class ResellerApplicationStatus")]
        self.assertIn("billing_mode", block)
        self.assertNotIn("commission_percent", block)

    def test_session_alters_billing_mode(self):
        src = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("reseller_plans ADD COLUMN billing_mode", src)

    def test_make_reseller_accepts_billing_mode(self):
        src = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
        fn = src[src.find("async def make_reseller") : src.find("async def list_active_reseller_plans")]
        # make_reseller is before list - find differently
        fn = src[src.find("async def make_reseller") : src.find("async def make_reseller") + 3500]
        self.assertIn("billing_mode", fn)


class NeutralRowActionsTests(unittest.TestCase):
    def test_css_neutralizes_ops_except_danger_warn(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".row-actions-menu .btn:not(.btn-danger):not(.btn-warn)", css)


class VersionTests(unittest.TestCase):
    def test_version_files_match(self):
        from app.version import __version__

        file_ver = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(__version__, file_ver)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
