"""3.6.8 — ticket notify actually delivers; no miniapp on shop bots."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class DispatchDualNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def _dispatch(self, *, live_token: str, shop_token: str, key: str,
                        notify_on: bool = True):
        from app.services import notifications

        live_bot = MagicMock(token=live_token)
        other_bot = MagicMock()
        other_bot.session.close = AsyncMock()
        profile = MagicMock(bot_token=shop_token)
        with (
            patch.object(notifications, "_resolve_shop_reseller_id", AsyncMock(return_value=7)),
            patch.object(notifications, "notify_enabled", AsyncMock(return_value=notify_on)),
            patch.object(notifications, "_shop_recipient_chat_ids", AsyncMock(return_value=[111])),
            patch.object(notifications, "_send_to_chats", AsyncMock(return_value=1)) as send,
            patch(
                "app.services.resellers.get_reseller_profile",
                AsyncMock(return_value=profile),
            ),
            patch(
                "app.services.reseller_bots.open_notify_bot_for_reseller",
                AsyncMock(return_value=(other_bot, True)),
            ) as open_bot,
        ):
            count = await notifications._dispatch_dual_notify(
                live_bot, AsyncMock(), key, "text"
            )
        return count, live_bot, other_bot, send, open_bot

    async def test_dispatch_prefers_live_shop_bot(self):
        count, live_bot, _other, send, open_bot = await self._dispatch(
            live_token="123:abc", shop_token="123:abc", key="notify_new_ticket"
        )

        self.assertEqual(count, 1)
        open_bot.assert_not_awaited()
        self.assertIs(send.await_args.args[0], live_bot)

    async def test_dispatch_opens_shop_bot_when_live_bot_is_another_shop(self):
        count, _live, other_bot, send, open_bot = await self._dispatch(
            live_token="123:abc", shop_token="999:zzz", key="notify_new_ticket"
        )

        self.assertEqual(count, 1)
        open_bot.assert_awaited_once()
        self.assertIs(send.await_args.args[0], other_bot)
        other_bot.session.close.assert_awaited_once()

    async def test_disabled_shop_pref_blocks_orders_but_not_ticket_dms(self):
        order_count, *_ = await self._dispatch(
            live_token="1:a", shop_token="1:a", key="notify_new_order", notify_on=False
        )
        ticket_count, *_ = await self._dispatch(
            live_token="1:a", shop_token="1:a", key="notify_new_ticket", notify_on=False
        )

        self.assertEqual(order_count, 0)
        self.assertEqual(ticket_count, 1)


if __name__ == "__main__":
    unittest.main()
