"""Staff reports and alerts carry a button to the screen to follow up from, opened under the notice."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.panel_history import panel_history
from app.db.models import BotUser
from app.services import backup_telegram, pg_link_alerts, reseller_purchase_notify
from app.services.notice_links import NOTICE_CLOSE_CALLBACK, opened_screen
from app.services.replaceable_notices import DAILY_REPORT_KEY, send_replacing_report
from app.services.staff_notice_buttons import (
    pasarguard_markup,
    platform_report_markup,
    reseller_card_markup,
    shop_report_markup,
    subordinate_report_markup,
)
from tests.chat_sim import ChatSim


def _callbacks(markup: InlineKeyboardMarkup | None) -> list[str]:
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


class ButtonTests(unittest.TestCase):
    def test_every_button_opens_its_screen_under_the_notice(self) -> None:
        expected = {
            "adm:dash": platform_report_markup(),
            "nv:res:home": shop_report_markup(),
            "nv:adm:resellers": subordinate_report_markup(shop=False),
            "nv:adm:pg": pasarguard_markup(),
            "adm:resellers:view:12": reseller_card_markup(12),
        }
        for screen, markup in expected.items():
            with self.subTest(screen=screen):
                (data,) = _callbacks(markup)
                self.assertEqual(opened_screen(data), screen)

    def test_a_shops_digest_opens_the_shops_own_panel(self) -> None:
        (data,) = _callbacks(subordinate_report_markup(shop=True))
        self.assertEqual(opened_screen(data), "nv:res:home")


class SendPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_report_button_is_wrapped_before_sending(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock())
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊", callback_data="adm:dash")]])
        with patch("app.services.replaceable_notices.replace_notices", AsyncMock()):
            await send_replacing_report(MagicMock(), bot, [5], "report", replace_key=DAILY_REPORT_KEY, reply_markup=markup)
        self.assertEqual(_callbacks(bot.send_message.await_args.kwargs["reply_markup"]), ["nto:adm:dash"])

    async def test_a_report_without_buttons_is_sent_as_before(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock())
        with patch("app.services.replaceable_notices.replace_notices", AsyncMock()):
            await send_replacing_report(MagicMock(), bot, [5], "report", replace_key=DAILY_REPORT_KEY, parse_mode="HTML")
        self.assertNotIn("reply_markup", bot.send_message.await_args.kwargs)

    async def test_the_link_alert_opens_the_pasarguard_hub(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.session.close = AsyncMock()
        settings = MagicMock(admin_ids=[7])
        with patch("app.bot.create_bot", return_value=bot), patch.object(pg_link_alerts, "get_settings", return_value=settings):
            self.assertEqual(await pg_link_alerts._send_to_owners("down"), 1)
        self.assertEqual(_callbacks(bot.send_message.await_args.kwargs["reply_markup"]), ["nto:nv:adm:pg"])

    async def test_the_backup_problem_opens_the_backup_screen_under_the_notice(self) -> None:
        destination = backup_telegram.Destination("1:A", [7], custom=False)
        with patch.object(backup_telegram, "resolve_destination", AsyncMock(return_value=destination)), \
                patch.object(backup_telegram, "_send_text", AsyncMock(return_value=1)) as send:
            await backup_telegram.notify_backup_problem(MagicMock(), "failed")
        self.assertEqual(_callbacks(send.await_args.kwargs["markup"]), ["nto:nv:adm:backup"])

    async def test_a_separate_backup_bot_gets_no_button_it_cannot_open(self) -> None:
        destination = backup_telegram.Destination("1:A", [7], custom=True)
        with patch.object(backup_telegram, "resolve_destination", AsyncMock(return_value=destination)), \
                patch.object(backup_telegram, "_send_text", AsyncMock(return_value=1)) as send:
            await backup_telegram.notify_backup_problem(MagicMock(), "failed")
        self.assertIsNone(send.await_args.kwargs["markup"])

    async def test_a_reseller_purchase_opens_the_resellers_card(self) -> None:
        buyer = BotUser(id=44, telegram_id=440, referral_code="b44", full_name="Reseller", role="reseller")
        bot = MagicMock()
        bot.session.close = AsyncMock()
        with patch("app.bot.create_bot", return_value=bot), \
                patch.object(reseller_purchase_notify, "_dispatch_dual_notify", AsyncMock(return_value=1)) as dispatch:
            await reseller_purchase_notify.notify_owner_of_reseller_purchase(
                MagicMock(), buyer=buyer, title="➕ ظرفیت", detail_lines=[], amount=1000,
            )
        self.assertEqual(_callbacks(dispatch.await_args.kwargs["markup"]), ["nto:adm:resellers:view:44"])


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sim = ChatSim(5501)
        await self.sim.start(owner=True)
        self.addAsyncCleanup(self.sim.stop)

    async def test_the_dashboard_opens_under_the_report_and_back_closes_it(self) -> None:
        async with self.sim.Session() as session:
            await send_replacing_report(
                session, self.sim.bot, [self.sim.telegram_id], "📈 گزارش روزانه",
                replace_key=DAILY_REPORT_KEY, reply_markup=platform_report_markup(),
            )
        report_id = max(self.sim.telegram.messages)
        await self.sim.tap("📊 داشبورد")

        panel = [m for m in self.sim.telegram.visible() if m.message_id > report_id]
        self.assertEqual(len(panel), 1, self.sim.screen())
        self.assertIn("داشبورد", panel[0].text)
        self.assertEqual(self.sim.telegram.messages[report_id].text, "📈 گزارش روزانه")
        backs = [data for row in panel[0].inline for text, data in row if "بازگشت" in text]
        self.assertEqual(backs, [NOTICE_CLOSE_CALLBACK])


if __name__ == "__main__":
    unittest.main()
