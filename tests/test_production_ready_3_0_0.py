"""Regression guards for Production Ready 3.0.0 hardening."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pasarguard import _sanitize_subscription_url, user_subscription_url


class VersionThreeConsistencyTests(unittest.TestCase):
    def test_version_files_aligned(self):
        from app.version import __version__

        self.assertEqual(__version__, "3.2.4")
        self.assertEqual(Path("VERSION").read_text(encoding="utf-8").strip(), "3.2.4")
        notes = Path("app/services/release_notes.py").read_text(encoding="utf-8")
        first = notes.split("RELEASE_NOTES_FA", 1)[1]
        self.assertLess(first.find('"3.2.4"'), first.find('"3.2.3"'))
        self.assertLess(first.find('"3.2.3"'), first.find('"3.0.5"'))
        self.assertLess(first.find('"3.0.5"'), first.find('"3.0.4"'))
        self.assertLess(first.find('"3.0.4"'), first.find('"3.0.3"'))
        self.assertLess(first.find('"3.0.3"'), first.find('"3.0.2"'))


class WalletDebitGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_debit_does_not_credit(self):
        from app.db.models import OrderStatus
        from app.services.orders import pay_with_wallet

        order = MagicMock()
        order.id = 9
        order.amount = 5000
        order.status = OrderStatus.PENDING.value
        order.note = None

        user = MagicMock()
        user.id = 1
        session = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=session)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "app.services.orders.debit_wallet",
                new=AsyncMock(side_effect=ValueError("موجودی کافی نیست")),
            ),
            patch("app.services.orders.credit_wallet", new=AsyncMock()) as credit,
            patch("app.services.orders.deliver_order", new=AsyncMock()),
        ):
            with self.assertRaises(ValueError):
                await pay_with_wallet(session, order, user)

        credit.assert_not_awaited()


class SubscriptionUrlSanitizeTests(unittest.TestCase):
    def test_rejects_javascript(self):
        self.assertIsNone(_sanitize_subscription_url("javascript:alert(1)"))
        self.assertIsNone(_sanitize_subscription_url("data:text/html,x"))
        self.assertIsNone(user_subscription_url({"subscription_url": "javascript:alert(1)"}))

    def test_allows_https_and_vpn_schemes(self):
        self.assertEqual(
            _sanitize_subscription_url("https://example.com/sub/abc"),
            "https://example.com/sub/abc",
        )
        self.assertTrue(_sanitize_subscription_url("vless://uuid@host:443") or False)


class MiniAppSecurityTests(unittest.TestCase):
    def test_miniapp_escapes_and_uses_dom(self):
        src = Path("app/web/templates/miniapp.html").read_text(encoding="utf-8")
        self.assertIn("function esc(", src)
        self.assertIn("createTextNode", src)
        self.assertIn("safeUrl", src)
        self.assertNotIn("onclick=\"showSvc(", src)
        self.assertNotIn("tg.openLink(data.service.url)", src)

    def test_csp_allows_telegram_for_miniapp(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/miniapp")', src)
        self.assertIn("https://telegram.org", src)


class PerfHardeningSourceTests(unittest.TestCase):
    def test_settings_save_uses_bulk(self):
        app_src = Path("app/api/app.py").read_text(encoding="utf-8")
        # settings tab save path batches into payload + set_settings_bulk
        self.assertIn("await set_settings_bulk(session, payload)", app_src)
        shop_src = Path("app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn("set_settings_bulk(session, payload, reseller_id=rid)", shop_src)

    def test_scheduler_concurrency_and_coalesce(self):
        src = Path("app/jobs/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("Semaphore", src)
        self.assertIn("max_instances=1", src)
        self.assertIn("coalesce=True", src)

    def test_sqlite_foreign_keys_enabled(self):
        src = Path("app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("PRAGMA foreign_keys=ON", src)
        self.assertIn("_ensure_indexes", src)

    def test_discount_reserved_at_create(self):
        src = Path("app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("_reserve_discount_code", src)
        self.assertIn("await _reserve_discount_code", src)
        self.assertNotIn("await _consume_discount_code", src)

    def test_gitignore_covers_env_backups(self):
        src = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env.bak", src)
        self.assertIn(".env.restored.*", src)


if __name__ == "__main__":
    unittest.main()
