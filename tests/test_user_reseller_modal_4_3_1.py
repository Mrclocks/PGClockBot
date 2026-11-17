"""v4.3.1 — reseller/user modal cleanup, dual notify, pending-order purge."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class PendingOrderCleanupTests(unittest.TestCase):
    def test_legacy_cancel_keys_stay_off_settings_tabs(self):
        from app.services.users import DEFAULT_SETTINGS, TAB_SETTING_GROUPS, keys_for_tab

        # Soft-cancel defaults remain so disable_all_legacy_soft_cancel can read them;
        # the finance cleanup modal no longer edits these fields.
        self.assertIn("pending_order_cleanup_enabled", DEFAULT_SETTINGS)
        self.assertIn("pending_order_ttl_hours", DEFAULT_SETTINGS)
        self.assertNotIn("cleanup", TAB_SETTING_GROUPS)
        self.assertNotIn("pending_order_cleanup_enabled", keys_for_tab("cleanup"))
        self.assertNotIn("pending_order_cleanup_enabled", keys_for_tab("payment"))

    def test_soft_cancel_is_not_an_operational_scheduler_job(self):
        from app.jobs import scheduler as jobs

        from tests.test_scheduler_background_jobs import OPERATIONS

        self.assertNotIn("pending_order_cleanup", OPERATIONS)
        self.assertIn("order_purge", OPERATIONS)
        self.assertTrue(hasattr(jobs, "run_auto_order_purge"))
        self.assertFalse(hasattr(jobs, "cleanup_stale_pending_orders"))


class DualNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_to_user_chat_tries_main_and_shop(self):
        from app.db.models import BotUser
        from app.services.notifications import _send_to_user_chat

        user = BotUser(
            id=7, telegram_id=555, full_name="U", role="user", username="u", reseller_id=3
        )
        session = AsyncMock()
        main_bot = MagicMock()
        main_bot.token = "MAIN"
        main_bot.send_message = AsyncMock()
        main_bot.session = MagicMock()
        main_bot.session.close = AsyncMock()
        shop_bot = MagicMock()
        shop_bot.token = "SHOP"
        shop_bot.send_message = AsyncMock()

        with patch("app.bot.create_bot", return_value=main_bot), patch(
            "app.services.reseller_bots.open_notify_bot_for_reseller",
            new=AsyncMock(return_value=(shop_bot, False)),
        ):
            ok = await _send_to_user_chat(session, user, "<b>hi</b>")
        self.assertTrue(ok)
        main_bot.send_message.assert_awaited()
        shop_bot.send_message.assert_awaited()


if __name__ == "__main__":
    unittest.main()
