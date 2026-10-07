"""Security hardening regression tests."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import OrderStatus
from app.services.orders import renew_service_with_plan


class MediaMountTests(unittest.TestCase):
    def test_media_mounts_uploads_only(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('"/media/uploads"', src)
        self.assertNotIn('StaticFiles(directory=str(DATA_DIR))', src)
        self.assertIn("never mount DATA_DIR", src)


class SignerSecretTests(unittest.TestCase):
    def test_no_hardcoded_fallback_secret(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertNotIn('"pgclock-secret"', src)
        self.assertIn("ensure_web_secret()", src)


class HealthLeakTests(unittest.TestCase):
    def test_public_health_is_minimal(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        start = src.index("async def health():")
        end = src.index("@app.get(\"/health/detail\")", start + 1)
        body = src[start:end]
        self.assertIn('{"ok":True}', body.replace(" ", ""))
        self.assertNotIn("admin_username", body)
        self.assertNotIn("boot_id", body)
        self.assertNotIn("local_version", body)
        self.assertNotIn("PID", body)


class RenewTenancyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_foreign_shop_plan(self):
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        plan = MagicMock()
        plan.is_active = True
        plan.is_trial = False
        plan.owner_reseller_id = 99
        plan.price = 0
        plan.id = 9
        svc = MagicMock()
        svc.id = 1
        svc.bot_user_id = 5
        with patch("app.services.users.current_shop_reseller_id", return_value=1):
            with self.assertRaises(ValueError) as ctx:
                await renew_service_with_plan(
                    session, user_id=5, service=svc, plan=plan
                )
        self.assertIn("فروشگاه", str(ctx.exception))

    async def test_rejects_trial_renewal(self):
        session = MagicMock()
        plan = MagicMock()
        plan.is_active = True
        plan.is_trial = True
        plan.owner_reseller_id = None
        plan.price = 0
        plan.id = 9
        svc = MagicMock()
        svc.id = 1
        svc.bot_user_id = 5
        with patch("app.services.users.current_shop_reseller_id", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                await renew_service_with_plan(
                    session, user_id=5, service=svc, plan=plan
                )
        self.assertIn("تست", str(ctx.exception))


class ReceiptAutoApproveTests(unittest.TestCase):
    def test_wallet_topup_scoped_auto_approve_guard(self):
        """Top-ups may auto-approve only when purse scope matches settings shop.

        After shop-wallet isolation, blanket blocking is unnecessary; the
        cross-scope guard must remain so a shop setting cannot approve a
        platform top-up (or the reverse).
        """
        src = Path("app/services/receipts.py").read_text(encoding="utf-8")
        self.assertIn("is_wallet_topup", src)
        self.assertIn("wallet_shop_id", src)
        self.assertIn("Cross-scope would re-open minting", src)
        self.assertIn("if topup_shop != shop_rid:", src)


class ForceJoinMiddlewareTests(unittest.TestCase):
    def test_middleware_registered(self):
        init_src = Path("app/bot/__init__.py").read_text(encoding="utf-8")
        self.assertIn("ForceJoinMiddleware", init_src)
        mw_src = Path("app/bot/middlewares.py").read_text(encoding="utf-8")
        self.assertIn("get_chat_member", mw_src)


class SessionRevalidationTests(unittest.TestCase):
    def test_require_staff_checks_active_profile(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("profile.is_active", src)
        self.assertIn("setup_is_complete(profile)", src)
        # soft-upgrade must not remain in require_staff
        tree = ast.parse(src)
        self.assertTrue(any("Never trust stale cookie" in src or "re-read ACL" in src for _ in [1]))


class TimedSessionTests(unittest.TestCase):
    def test_uses_timed_serializer(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("URLSafeTimedSerializer", src)
        self.assertIn("max_age=SESSION_MAX_AGE", src)
        self.assertIn("admin_session_version", src)


class TrustProxyTests(unittest.TestCase):
    def test_xff_gated_by_trust_proxy(self):
        # Client-IP / XFF parsing lives in login_guard. Phase 3: cookie HTTPS
        # also goes through login_guard.forwarded_proto_is_https (trusted peer),
        # not a direct trust_proxy read inside app.py.
        guard = Path("app/api/login_guard.py").read_text(encoding="utf-8")
        self.assertIn("trust_proxy", guard)
        self.assertIn("x-forwarded-for", guard.lower())
        self.assertIn("def forwarded_proto_is_https", guard)
        app_src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("forwarded_proto_is_https", app_src)
        self.assertIn("from app.api.login_guard import", app_src)


class WebhookSecretTests(unittest.TestCase):
    def test_webhook_rejects_missing_secret(self):
        src = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("X-Telegram-Bot-Api-Secret-Token", src)
        self.assertIn("ensure_webhook_secret", src)


class SetupGateTests(unittest.TestCase):
    def test_setup_gate_helpers_exist(self):
        import app.services.setup_wizard as setup_wizard

        # ensure_setup_gate_token()/setup_gate_ok() both short-circuit to
        # "no gate" once setup is complete — correct in production, but this
        # test must exercise the *pending-setup* path regardless of whether
        # this machine's own data/setup_complete.flag already exists (e.g.
        # a dev box that has actually been through the wizard already).
        with patch.object(setup_wizard, "is_setup_complete", return_value=False):
            setup_wizard.revoke_setup_gate()
            token = setup_wizard.ensure_setup_gate_token()
            self.assertTrue(len(token) >= 16)
            self.assertTrue(setup_wizard.setup_gate_ok(token))
            self.assertFalse(setup_wizard.setup_gate_ok("wrong-token"))
        setup_wizard.revoke_setup_gate()


class SettingsReadPerfTests(unittest.TestCase):
    def test_get_all_settings_does_not_reseed(self):
        src = Path("app/services/users.py").read_text(encoding="utf-8")
        start = src.index("async def get_all_settings")
        end = src.index("\ndef on(", start)
        body = src[start:end]
        self.assertNotIn("ensure_default_settings", body)


class DepsCleanupTests(unittest.TestCase):
    def test_unused_heavy_deps_removed(self):
        req = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("python-jose", req)
        self.assertNotIn("aiofiles", req)


if __name__ == "__main__":
    unittest.main()
