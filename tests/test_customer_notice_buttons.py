"""Customer notices carry a button to what they are about, opened under the notice."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardMarkup

from app.bot.panel_history import panel_history
from app.db.models import BotUser
from app.services import manual_adjust_notices, payer_notify, stuck_order_resolution
from app.services.customer_notice_buttons import customer_notice_markup
from app.services.notice_links import NOTICE_CLOSE_CALLBACK, opened_screen
from app.services.notifications import _account_edit_markup, _send_to_chats
from app.services.support_button import SUPPORT_HOME_CALLBACK, support_markup
from tests.chat_sim import ChatSim


def _screens(markup: InlineKeyboardMarkup | None) -> list[str | None]:
    return [opened_screen(b.callback_data) for row in (markup.inline_keyboard if markup else []) for b in row]


class MarkupTests(unittest.TestCase):
    def test_each_button_opens_its_screen_under_the_notice(self) -> None:
        markup = customer_notice_markup(wallet=True, club=True, services=True, support=True)
        self.assertEqual(_screens(markup), ["nv:w:home", "nv:loy:home", "svc:list", SUPPORT_HOME_CALLBACK])

    def test_no_button_asked_means_no_keyboard(self) -> None:
        self.assertIsNone(customer_notice_markup())

    def test_only_a_revoked_agency_offers_support(self) -> None:
        self.assertEqual(_screens(_account_edit_markup("reseller_revoke")), [SUPPORT_HOME_CALLBACK])
        for event in ("block", "unblock", "role", "user_delete"):
            with self.subTest(event=event):
                self.assertIsNone(_account_edit_markup(event))


class SendPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_payer_notice_opens_its_buttons_under_the_notice(self) -> None:
        user = BotUser(id=3, telegram_id=30, referral_code="p3")
        session = MagicMock()
        session.get = AsyncMock(return_value=user)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        markup = InlineKeyboardMarkup.model_validate({"inline_keyboard": [[{"text": "w", "callback_data": "nv:w:home"}]]})
        with patch("app.services.reseller_bots.open_notify_bot_for_user", AsyncMock(return_value=(bot, False))):
            self.assertTrue(await payer_notify.notify_payer(session, 3, title="t", body="b", reply_markup=markup))
        self.assertEqual(_screens(bot.send_message.await_args.kwargs["reply_markup"]), ["nv:w:home"])

    async def test_a_refund_offers_the_wallet_and_support(self) -> None:
        result = SimpleNamespace(refunded=5000, user_id=3, order_id=9, shop_id=None)
        with patch.object(stuck_order_resolution, "notify_payer", AsyncMock()) as notify:
            await stuck_order_resolution.tell_customer_refunded(MagicMock(), result)
        self.assertEqual(_screens(notify.await_args.kwargs["reply_markup"]), ["nv:w:home", SUPPORT_HOME_CALLBACK])

    async def test_a_manual_wallet_change_opens_the_wallet_and_points_open_the_club(self) -> None:
        with patch.object(manual_adjust_notices, "_send_customer_notice", AsyncMock()) as send:
            manual_adjust_notices.schedule_wallet_notice(user_id=3, mode="credit", amount=10, balance=20)
            manual_adjust_notices.schedule_points_notice(user_id=3, mode="credit", points=1, balance=2)
            await asyncio.sleep(0)
        wallet, points = (call.kwargs["markup"] for call in send.await_args_list)
        self.assertEqual(_screens(wallet), ["nv:w:home"])
        self.assertEqual(_screens(points), ["nv:loy:home"])

    async def test_a_replayed_adjust_sends_nothing(self) -> None:
        with patch.object(manual_adjust_notices, "_send_customer_notice", AsyncMock()) as send:
            manual_adjust_notices.schedule_wallet_notice(user_id=3, mode="credit", amount=10, balance=20, replayed=True)
            await asyncio.sleep(0)
        send.assert_not_awaited()


class CustomerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sim = ChatSim(5701)
        await self.sim.start(owner=False)
        self.addAsyncCleanup(self.sim.stop)
        await self.sim.type("/start")

    async def test_support_opens_under_a_rejected_payment_and_back_returns_to_it(self) -> None:
        await _send_to_chats(self.sim.bot, [self.sim.telegram_id], "❌ پرداخت رد شد", markup=support_markup())
        notice_id = max(self.sim.telegram.messages)
        await self.sim.tap("پشتیبانی")

        panel = [m for m in self.sim.telegram.visible() if m.message_id > notice_id]
        self.assertEqual(len(panel), 1, self.sim.screen())
        self.assertEqual(self.sim.telegram.messages[notice_id].text, "❌ پرداخت رد شد")
        self.assertFalse(self.sim.telegram.messages[notice_id].deleted)
        backs = [data for row in panel[0].inline for text, data in row if "بازگشت" in text]
        self.assertIn(NOTICE_CLOSE_CALLBACK, backs)

    async def test_the_wallet_opens_under_a_refund_notice(self) -> None:
        await _send_to_chats(
            self.sim.bot, [self.sim.telegram_id], "↩️ بازگشت وجه", markup=customer_notice_markup(wallet=True),
        )
        notice_id = max(self.sim.telegram.messages)
        await self.sim.tap("کیف پول")

        panel = [m for m in self.sim.telegram.visible() if m.message_id > notice_id]
        self.assertEqual(len(panel), 1, self.sim.screen())
        self.assertIn("کیف پول", panel[0].text)


if __name__ == "__main__":
    unittest.main()
