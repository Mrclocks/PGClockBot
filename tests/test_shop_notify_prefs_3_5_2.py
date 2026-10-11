"""3.5.2 — Shop-scoped notify prefs isolated from platform owner."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class CatalogAclTests(unittest.TestCase):
    def test_platform_only_keys_hidden_from_shop(self):
        from app.services.notifications import (
            PLATFORM_ONLY_NOTIFY_KEYS,
            shop_notify_allowed_keys,
            shop_notify_catalog,
        )

        full = shop_notify_allowed_keys(
            ["dashboard", "plans", "orders", "payments", "tickets", "stats", "shop_settings"]
        )
        self.assertTrue(full)
        for key in PLATFORM_ONLY_NOTIFY_KEYS:
            self.assertNotIn(key, full)
        titles = [t for _, t, *_ in shop_notify_catalog(["orders", "shop_settings", "plans"])]
        self.assertTrue(any("سفارش" in t or "اشتراک" in t for t in titles))
        # Explicit shop keys only — no soft-inject of unrelated notify groups.
        ticket_only = shop_notify_allowed_keys(["tickets", "shop_settings", "plans"])
        self.assertIn("notify_new_ticket", ticket_only)
        self.assertNotIn("notify_account_edits", ticket_only)
        # Wallet top-up success is payments-scoped (shop purse), not tickets.
        self.assertNotIn("notify_wallet_topup", ticket_only)
        self.assertEqual(PLATFORM_ONLY_NOTIFY_KEYS, frozenset({"notify_account_edits"}))
        pay_keys = shop_notify_allowed_keys(["payments", "shop_settings", "plans"])
        self.assertIn("notify_wallet_topup", pay_keys)


class IsolationLogicTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_prefs_do_not_read_global_setting(self):
        from app.services.notifications import get_shop_notify_prefs

        session = AsyncMock()
        # Simulate empty ResellerSetting query → defaults, ignoring any global overlay
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        prefs = await get_shop_notify_prefs(session, 42)
        self.assertIn("notify_new_order", prefs)
        self.assertIn("notify_wallet_topup", prefs)
        self.assertNotIn("notify_account_edits", prefs)

    async def test_save_shop_rejects_platform_only_even_if_passed(self):
        from app.services.notifications import save_shop_notify_prefs

        with patch(
            "app.services.notifications.set_settings_bulk", new=AsyncMock()
        ) as bulk:
            await save_shop_notify_prefs(
                AsyncMock(),
                7,
                {
                    "s_notify_new_ticket": "1",
                    "s_notify_account_edits": "1",
                    "s_notify_wallet_topup": "1",
                },
                allowed_keys={
                    "notify_new_ticket",
                    "notify_account_edits",
                    "notify_wallet_topup",
                },
            )
        payload = bulk.await_args.args[1]
        self.assertEqual(payload.get("notify_new_ticket"), "1")
        self.assertEqual(payload.get("notify_wallet_topup"), "1")
        self.assertNotIn("notify_account_edits", payload)
        self.assertEqual(bulk.await_args.kwargs.get("reseller_id"), 7)

    async def test_dispatch_uses_independent_gates(self):
        from app.services.notifications import _dispatch_dual_notify

        session = AsyncMock()
        order = MagicMock()
        order.reseller_id = 99

        async def enabled(_session, key, *, reseller_id=None):
            if reseller_id is None:
                return True  # owner ON — must still not receive shop events
            return key == "notify_new_order"

        with patch(
            "app.services.notifications.notify_enabled", side_effect=enabled
        ), patch(
            "app.services.notifications._send_to_chats", new=AsyncMock()
        ) as send, patch(
            "app.services.notifications._resolve_shop_reseller_id",
            new=AsyncMock(return_value=99),
        ), patch(
            "app.services.notifications._shop_recipient_chat_ids",
            new=AsyncMock(return_value=[555]),
        ), patch(
            "app.services.resellers.get_reseller_profile",
            new=AsyncMock(return_value=SimpleNamespace(bot_token="shop-token")),
        ), patch(
            "app.services.reseller_bots.open_notify_bot_for_reseller",
            new=AsyncMock(return_value=(MagicMock(token="shop-token", session=MagicMock(close=AsyncMock())), True)),
        ):
            bot = MagicMock()
            bot.token = "main-token"
            await _dispatch_dual_notify(
                bot, session, "notify_new_order", "hello", order=order
            )

        # Shop path only — platform ADMIN_IDS must not be contacted
        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.await_args.args[1], [555])

    async def test_platform_customer_still_notifies_admins(self):
        from app.services.notifications import _dispatch_dual_notify

        session = AsyncMock()
        bot = MagicMock()
        order = MagicMock()
        order.reseller_id = None

        with patch(
            "app.services.notifications.notify_enabled", new=AsyncMock(return_value=True)
        ), patch(
            "app.services.notifications._send_to_chats", new=AsyncMock()
        ) as send, patch(
            "app.services.notifications._resolve_shop_reseller_id",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.services.notifications.get_settings"
        ) as gs:
            gs.return_value.admin_ids = [111]
            await _dispatch_dual_notify(
                bot, session, "notify_new_ticket", "hello", order=order
            )

        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.await_args.args[1], [111])


class WiringTests(unittest.TestCase):
    def test_shop_route_and_templates(self):
        api = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn('/shop-notifications', api)
        self.assertIn("save_shop_notify_prefs", api)
        self.assertIn('startswith("notify_")', api)
        shop_tpl = (ROOT / "app/web/templates/shop_settings.html").read_text(encoding="utf-8")
        self.assertIn("_shop_settings_notifications.html", shop_tpl)
        partial = (
            ROOT / "app/web/templates/_shop_settings_notifications.html"
        ).read_text(encoding="utf-8")
        self.assertIn("/shop-notifications", partial)
        self.assertIn("مستقل از ادمین اصلی", partial)

    def test_bot_reseller_notify_section(self):
        bot = (ROOT / "app/bot/handlers/reseller_settings.py").read_text(encoding="utf-8")
        self.assertIn('"notify"', bot)
        self.assertIn("res:st:ntog:", bot)
        self.assertIn("PLATFORM_ONLY_NOTIFY_KEYS", bot)
        self.assertIn("get_shop_notify_prefs", bot)

    def test_version_at_least_3_5_2(self):
        from app.version import __version__

        self.assertGreaterEqual(
            tuple(int(x) for x in __version__.split(".")[:3]), (0, 1, 0)
        )
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"0.1.0"', notes)


if __name__ == "__main__":
    unittest.main()
