"""Bot user-facing errors must be clear Persian, not raw English API noise."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.credential_policy import friendly_pg_error
from app.services.pasarguard import PasarGuardError
from app.services.redact import user_safe_error

ROOT = Path(__file__).resolve().parents[1]


class FriendlyBotErrorTests(unittest.TestCase):
    def test_duplicate_and_timeout_persian(self):
        self.assertIn("قبلاً ثبت شده", friendly_pg_error("User already exists"))
        self.assertIn(
            "طول کشید",
            friendly_pg_error("httpx.ReadTimeout: timed out"),
        )
        self.assertIn(
            "برقرار نشد",
            friendly_pg_error("Connection refused"),
        )

    def test_keeps_existing_persian(self):
        fa = "موجودی کیف پول کافی نیست"
        self.assertEqual(friendly_pg_error(fa), fa)

    def test_user_safe_error_pasar_guard(self):
        exc = PasarGuardError("POST /api/user failed (409)", 409, {"detail": "already exists"})
        out = user_safe_error(exc)
        self.assertIn("قبلاً ثبت شده", out)
        self.assertNotIn("failed", out.lower())

    def test_user_safe_error_plain_value_error_persian(self):
        out = user_safe_error(ValueError("پلن یافت نشد"))
        self.assertEqual(out, "پلن یافت نشد")


class BotHandlerErrorSourceGuards(unittest.TestCase):
    def test_handlers_prefer_user_safe_error(self):
        paths = [
            "app/bot/handlers/admin.py",
            "app/bot/handlers/admin_pg_users.py",
            "app/bot/handlers/admin_pg_nodes.py",
            "app/bot/handlers/shop.py",
            "app/bot/handlers/reseller.py",
            "app/bot/handlers/services.py",
            "app/bot/handlers/loyalty.py",
            "app/bot/handlers/reply_nav.py",
            "app/bot/handlers/admin_backup.py",
        ]
        for rel in paths:
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("user_safe_error", src, rel)
            # No raw exception echo in Telegram answers.
            self.assertNotIn('answer(str(e)', src, rel)
            self.assertNotIn('f"خطا: {e}"', src, rel)


if __name__ == "__main__":
    unittest.main()
