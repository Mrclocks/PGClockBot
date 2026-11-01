"""Phase 0 + Phase 1 hardening — money safety, ACL chrome, redirect, shop scope."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SanitizeReturnToTests(unittest.TestCase):
    def test_blocks_open_redirects_and_control_chars(self):
        from app.services.table_bulk import sanitize_return_to

        self.assertEqual(sanitize_return_to("//evil.example", default="/inbox"), "/inbox")
        self.assertEqual(sanitize_return_to("/\\evil", default="/inbox"), "/inbox")
        self.assertEqual(sanitize_return_to("https://evil", default="/inbox"), "/inbox")
        self.assertEqual(sanitize_return_to("/inbox\nLocation: x", default="/inbox"), "/inbox")
        self.assertEqual(sanitize_return_to("/inbox?ok=1&err=2&_=9", default="/x"), "/inbox")
        self.assertTrue(
            sanitize_return_to("/finance?tab=orders", default="/x").startswith("/finance")
        )


class SoftInjectAclTests(unittest.TestCase):
    def test_subset_perms_are_not_force_expanded(self):
        from app.services.resellers import normalize_feature_perms, with_shop_settings

        self.assertEqual(with_shop_settings(["tickets"]), ["tickets", "dashboard"])
        self.assertNotIn("plans", with_shop_settings(["tickets"]))
        raw = normalize_feature_perms("")
        self.assertIn("plans", raw)
        self.assertIn("tickets", raw)


class OwnerChromeSourceTests(unittest.TestCase):
    def test_base_html_gates_owner_nav_on_is_owner(self):
        html = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("is_owner", html)
        self.assertIn("{% if is_owner %}", html)
        users_idx = html.find('href="/users"')
        self.assertGreater(users_idx, 0)
        window = html[max(0, users_idx - 120) : users_idx]
        self.assertIn("is_owner", window)
        self.assertNotIn("is_reseller or 'tickets' in perms", html)
        self.assertNotIn("'shop_settings' in perms or is_reseller", html)

    def test_render_injects_is_owner(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('ctx["is_owner"]', src)
        self.assertIn("is_explicit_owner_staff", src)

    def test_identity_help_has_owner_checklist(self):
        from app.services.platform_identity import identity_help_fa

        help_ = identity_help_fa("admin")
        blob = " ".join(help_.get("lines") or [])
        self.assertIn("مالک", blob)


class SetupPostgresQuoteTests(unittest.TestCase):
    def test_no_raw_password_interpolation(self):
        sh = (ROOT / "scripts/setup_postgres.sh").read_text(encoding="utf-8")
        # Passwords must go through format(%L); never raw PASSWORD '${DB_PASS}'.
        self.assertNotIn("PASSWORD '${DB_PASS}'", sh)
        self.assertNotIn('PASSWORD "${DB_PASS}"', sh)
        self.assertIn("%I", sh)
        self.assertIn("%L", sh)
        # Hex-only secrets + DO/EXECUTE (psql -v db_pass= is forbidden — it broke VPS TCP auth).
        self.assertIn("0-9a-fA-F", sh)
        self.assertIn("EXECUTE format(", sh)
        self.assertNotIn("-v db_pass=", sh)
        self.assertIn("-v", sh)  # ON_ERROR_STOP still OK
        # Must not leave md5 hashes while HBA prefers scram (v11.0.4 VPS failure).
        self.assertNotIn("password_encryption = 'md5'", sh)
        self.assertIn("local   all             postgres", sh)


class SetWebPasswordNoPlaintextTests(unittest.TestCase):
    def test_clears_env_password(self):
        src = (ROOT / "scripts/set_web_password.py").read_text(encoding="utf-8")
        self.assertIn('WEB_ADMIN_PASSWORD=""', src)
        self.assertIn("never persist plaintext", src.lower())


class TrialReleaseSourceTests(unittest.TestCase):
    def test_cancel_reject_stale_release_trial(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("def _trial_shop_key_for_order", src)
        self.assertIn("async def _release_trial_claim_for_order", src)
        stale = src[
            src.find("async def cancel_stale_pending_orders") : src.find(
                "async def cancel_stale_pending_for_settings"
            )
        ]
        # The stale cancel releases holds through the shared helper (also used by the purge).
        self.assertIn("release_unpaid_order_holds", stale)
        holds = src[src.find("async def release_unpaid_order_holds") : src.find("async def cancel_stale_pending_orders")]
        self.assertIn("_release_trial_claim_for_order", holds)
        self.assertNotIn('note.startswith("trial:")', stale)
        self.assertIn(
            "_release_trial_claim_for_order",
            src[src.find("async def cancel_order") :],
        )
        self.assertIn(
            "_release_trial_claim_for_order",
            src[src.find("async def reject_order") :],
        )
        self.assertIn(
            "_release_trial_claim_for_order",
            src[src.find("async def reject_payment") :],
        )


class WalletPayAtomicSourceTests(unittest.TestCase):
    def test_debit_commit_false_and_recovery(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def pay_with_wallet") : src.find("async def mark_order_free_paid")
        ]
        self.assertIn("commit=False", fn)
        self.assertIn("_resume_paid_wallet_order", fn)
        self.assertIn("crash", fn.lower())


class ChargeCodeScopeSourceTests(unittest.TestCase):
    def test_uses_current_shop_and_credit_wallet(self):
        src = (ROOT / "app/services/ux20.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def redeem_charge_code") : src.find("async def create_charge_code")
        ]
        self.assertIn("current_shop_reseller_id", fn)
        self.assertIn("credit_wallet", fn)
        self.assertIn("commit=False", fn)
        self.assertNotIn("user.wallet_balance =", fn)


class SchedulerShopScopeSourceTests(unittest.TestCase):
    def test_services_carry_the_shop_that_sold_them(self):
        from app.db.models import UserService

        columns = UserService.__table__.columns
        self.assertTrue(columns["shop_id"].nullable)
        self.assertFalse(columns["shop_needs_review"].nullable)


class BotTokenRedactSourceTests(unittest.TestCase):
    def test_bot_status_uses_redact(self):
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("redact(exc)", app_src)
        shop_src = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn("redact(exc)", shop_src)


class HomePagesReturnToSourceTests(unittest.TestCase):
    def test_uses_sanitize_return_to(self):
        src = (ROOT / "app/api/home_pages.py").read_text(encoding="utf-8")
        self.assertIn("sanitize_return_to", src)
        self.assertGreaterEqual(src.count("sanitize_return_to("), 2)


if __name__ == "__main__":
    unittest.main()
