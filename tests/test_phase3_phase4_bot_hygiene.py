"""Phase 3 + Phase 4 — bot admin chrome + ops/hygiene hardening."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


class BotAdminChromePhase3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_sticky_admin_without_admin_ids_gets_user_menu(self):
        from app.db.models import Role
        from app.services.reseller_access import effective_menu_role

        user = SimpleNamespace(id=1, role=Role.ADMIN.value, telegram_id=999001)
        session = AsyncMock()
        with (
            patch(
                "app.services.reseller_access.load_reseller_actor",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch("app.config.get_settings", return_value=SimpleNamespace(admin_ids=set())),
        ):
            role = await effective_menu_role(session, user, is_reseller_bot=False)
        self.assertEqual(role, Role.USER.value)

    async def test_admin_ids_member_gets_admin_menu(self):
        from app.db.models import Role
        from app.services.reseller_access import effective_menu_role

        user = SimpleNamespace(id=1, role=Role.USER.value, telegram_id=42)
        session = AsyncMock()
        with (
            patch(
                "app.services.reseller_access.load_reseller_actor",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch("app.config.get_settings", return_value=SimpleNamespace(admin_ids={42})),
        ):
            role = await effective_menu_role(session, user, is_reseller_bot=False)
        self.assertEqual(role, Role.ADMIN.value)


class UserSafeErrorPhase3Tests(unittest.TestCase):
    def test_redacts_bot_token_shaped_errors(self):
        from app.services.redact import user_safe_error

        raw = "Client error '401' for url 'https://api.telegram.org/bot123456:AAHdeadbeefdeadbeefdeadbeefdeadbee/getMe'"
        out = user_safe_error(raw)
        self.assertNotIn("AAHdeadbeef", out)
        # 401 maps to a clear Persian login message (token must never leak).
        self.assertIn("پاسارگارد", out)
        self.assertNotIn("bot123456", out)

    def test_fallback_on_traceback_like_text(self):
        from app.services.redact import user_safe_error

        out = user_safe_error('Traceback (most recent call last):\n  File "/app/bot/x.py"')
        self.assertEqual(out, "خطای داخلی. دوباره تلاش کنید.")


class Phase3Phase4SourceGuards(unittest.TestCase):
    def test_wallet_payments_shop_use_user_safe_error(self):
        wallet = (ROOT / "app/bot/handlers/wallet.py").read_text(encoding="utf-8")
        payments = (ROOT / "app/bot/handlers/payments.py").read_text(encoding="utf-8")
        shop = (ROOT / "app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn("user_safe_error", wallet)
        self.assertIn("user_safe_error", payments)
        self.assertIn("user_safe_error", shop)
        self.assertIn("user_safe_error(e)", wallet)
        self.assertIn('format_message("❌ خطا"', wallet)
        self.assertIn("user_safe_error(e)", payments)

    def test_scheduler_logs_shop_bot_close_failures(self):
        src = (ROOT / "app/jobs/scheduler.py").read_text(encoding="utf-8")
        idx = src.find("shop_bot.session.close")
        self.assertGreater(idx, 0)
        block = src[idx - 220 : idx + 260]
        self.assertIn("logger.debug", block)
        self.assertIn("session close failed", block)
        self.assertNotIn("except Exception:\n                            pass", block)

    def test_effective_menu_role_documents_phase3(self):
        src = (ROOT / "app/services/reseller_access.py").read_text(encoding="utf-8")
        self.assertIn("ADMIN_IDS", src[src.find("async def effective_menu_role") :])
        self.assertIn("Sticky", src[src.find("async def effective_menu_role") :])


if __name__ == "__main__":
    unittest.main()
