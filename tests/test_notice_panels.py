"""A button under a notice opens its screen in a new panel, and «بازگشت» returns to the notice.

The flow tests drive the real bot through ``tests.chat_sim``: a staff notice is sent the way
``_send_to_chats`` sends it, the person taps its buttons, and the test checks what stays on screen.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.notice_panels import NoticePanels, notice_panels
from app.db.models import BotUser
from app.services.notice_links import (
    CALLBACK_DATA_LIMIT,
    NOTICE_CLOSE_CALLBACK,
    notice_markup,
    notice_open_data,
    opened_screen,
)
from app.services.notifications import _send_to_chats, customer_markup, ticket_action_markup
from tests.chat_sim import ChatSim

NOTICE_TEXT = "🆕 اشتراک جدید"
BACK = "بازگشت"


def _callbacks(markup: InlineKeyboardMarkup | None) -> list[str]:
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


class NoticeLinkTests(unittest.TestCase):
    def test_screen_buttons_open_a_panel_and_actions_stay_on_the_notice(self) -> None:
        markup = ticket_action_markup(5, user_id=7, shop_id=None)
        self.assertEqual(
            _callbacks(notice_markup(markup)),
            ["nto:adm:users:view:7", "tkt:reply:5", "tkt:close:5"],
        )

    def test_review_buttons_are_never_wrapped(self) -> None:
        for data in ("payrev:ok:3", "payrev:no:3", "ordrev:ok:3", "payvoid:ask:3", "tkt:reply:1"):
            with self.subTest(data=data):
                self.assertEqual(notice_open_data(data), data)

    def test_wrapping_twice_changes_nothing(self) -> None:
        once = notice_markup(customer_markup(user_id=7, service_id=9, shop_id=None))
        self.assertEqual(_callbacks(notice_markup(once)), _callbacks(once))

    def test_data_that_would_pass_the_telegram_limit_is_left_as_it_is(self) -> None:
        long_data = "adm:users:svc:" + "9" * (CALLBACK_DATA_LIMIT - len("adm:users:svc:"))
        self.assertEqual(notice_open_data(long_data), long_data)

    def test_only_a_known_screen_is_opened(self) -> None:
        self.assertEqual(opened_screen("nto:res:user:4"), "res:user:4")
        self.assertIsNone(opened_screen("nto:payrev:ok:4"))
        self.assertIsNone(opened_screen("res:user:4"))
        self.assertIsNone(opened_screen(None))

    def test_url_buttons_are_kept(self) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐", url="https://x.example/a")]])
        self.assertIs(notice_markup(markup), markup)


class NoticePanelStoreTests(unittest.TestCase):
    def test_the_oldest_panel_is_forgotten_first(self) -> None:
        panels = NoticePanels(limit=2)
        for message_id in (1, 2, 3):
            panels.open((1, 1, message_id), f"res:user:{message_id}")
        self.assertIsNone(panels.get((1, 1, 1)))
        self.assertEqual(panels.get((1, 1, 3)).first_screen, "res:user:3")


class _NoticeFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        notice_panels.clear()
        self.addCleanup(notice_panels.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def send_notice(self, markup: InlineKeyboardMarkup | None) -> int:
        sent = await _send_to_chats(self.sim.bot, [self.sim.telegram_id], NOTICE_TEXT, markup=markup)
        self.assertEqual(sent, 1)
        return max(self.sim.telegram.messages)

    def notice(self, message_id: int):
        return self.sim.telegram.messages[message_id]

    def panels(self, notice_id: int) -> list:
        return [m for m in self.sim.telegram.visible() if m.from_bot and m.message_id > notice_id]


class PlatformNoticeTests(_NoticeFlow):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sim = ChatSim(5101)
        await self.sim.start(owner=True)
        self.addAsyncCleanup(self.sim.stop)
        async with self.sim.Session() as session:
            session.add(BotUser(id=60, telegram_id=6000000060, referral_code="n60", full_name="Customer Sixty"))
            await session.commit()

    async def test_the_customer_card_opens_under_the_notice_and_the_notice_stays(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=60, service_id=None, shop_id=None))
        await self.sim.tap("👤 کاربر")

        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)
        self.assertFalse(self.notice(notice_id).deleted)
        panels = self.panels(notice_id)
        self.assertEqual(len(panels), 1, self.sim.screen())
        self.assertIn("Customer Sixty", panels[0].text)

    async def test_back_on_the_first_screen_closes_the_panel_and_shows_the_notice(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=60, service_id=None, shop_id=None))
        await self.sim.tap("👤 کاربر")
        panel = self.panels(notice_id)[0]
        self.assertIn(NOTICE_CLOSE_CALLBACK, [data for row in panel.inline for _, data in row])

        await self.sim.tap(BACK)

        self.assertEqual(self.panels(notice_id), [], self.sim.screen())
        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)
        self.assertFalse(self.notice(notice_id).deleted)

    async def test_a_deeper_screen_returns_to_the_first_screen_and_then_to_the_notice(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=60, service_id=None, shop_id=None))
        await self.sim.tap("👤 کاربر")
        await self.sim.tap("🏷 تگ")
        self.assertIn("تگ ریسک", self.panels(notice_id)[0].text)

        await self.sim.tap(BACK)
        self.assertIn("Customer Sixty", self.panels(notice_id)[0].text)
        await self.sim.tap(BACK)

        self.assertEqual(self.panels(notice_id), [], self.sim.screen())
        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)

    async def test_after_an_action_on_the_first_screen_back_still_returns_to_the_notice(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=60, service_id=None, shop_id=None))
        await self.sim.tap("👤 کاربر")
        await self.sim.tap("🏷 تگ")
        panel = self.panels(notice_id)[0]
        tag_set = next(data for row in panel.inline for _, data in row if data.startswith("adm:users:tagset:"))
        await self.sim._tap(panel, tag_set)
        self.assertIn("Customer Sixty", self.panels(notice_id)[0].text)

        await self.sim.tap(BACK)

        self.assertEqual(self.panels(notice_id), [], self.sim.screen())
        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)

    async def test_the_same_screen_opened_from_the_menu_keeps_its_usual_back(self) -> None:
        await self.send_notice(customer_markup(user_id=60, service_id=None, shop_id=None))
        await self.sim.tap("👤 کاربر")
        panel = self.panels(0)[-1]
        await self.sim._tap(panel, "adm:users:list:0")
        users_list = self.sim.telegram.messages[panel.message_id]
        self.assertNotIn(NOTICE_CLOSE_CALLBACK, [data for row in users_list.inline for _, data in row])

    async def test_a_card_that_no_longer_exists_leaves_no_empty_panel(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=999, service_id=None, shop_id=None))
        await self.sim.tap("👤 کاربر")

        self.assertEqual(self.panels(notice_id), [], self.sim.screen())
        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)
        self.assertTrue(self.sim.telegram.alerts, "the person is told why nothing opened")


class ShopNoticeTests(_NoticeFlow):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sim = ChatSim(5201)
        await self.sim.start_shop(staff=True)
        self.addAsyncCleanup(self.sim.stop)
        for target, value in (
            ("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=False)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.sim.Session() as session:
            session.add_all([
                BotUser(id=70, telegram_id=7000000070, referral_code="s70", full_name="Shop Customer", reseller_id=900),
                BotUser(id=71, telegram_id=7000000071, referral_code="o71", full_name="Other Customer"),
            ])
            await session.commit()

    async def test_the_shop_customer_card_opens_under_the_notice(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=70, service_id=None, shop_id=900))
        await self.sim.tap("👤 کاربر")

        panels = self.panels(notice_id)
        self.assertEqual(len(panels), 1, self.sim.screen())
        self.assertIn("Shop Customer", panels[0].text)
        await self.sim.tap(BACK)
        self.assertEqual(self.panels(notice_id), [], self.sim.screen())

    async def test_another_shops_customer_is_refused_and_no_panel_is_left(self) -> None:
        notice_id = await self.send_notice(customer_markup(user_id=71, service_id=None, shop_id=900))
        await self.sim.tap("👤 کاربر")

        self.assertEqual(self.panels(notice_id), [], self.sim.screen())
        self.assertNotIn("Other Customer", " ".join(m.text for m in self.sim.telegram.visible()))
        self.assertTrue(self.sim.telegram.alerts)
        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)

    async def test_a_platform_screen_is_refused_on_a_shop_bot(self) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👤 کاربر", callback_data="adm:users:view:70"),
        ]])
        notice_id = await self.send_notice(markup)
        await self.sim.tap("👤 کاربر")

        self.assertEqual(self.panels(notice_id), [], self.sim.screen())
        self.assertEqual(self.notice(notice_id).text, NOTICE_TEXT)


if __name__ == "__main__":
    unittest.main()
