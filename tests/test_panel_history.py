"""«بازگشت» leads to the screen the person came from, and never repeats an action.

The flow tests drive the real bot through ``tests.chat_sim`` and read the back buttons of the panel.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.panel_history import (
    HISTORY_DEPTH,
    PanelHistories,
    Shown,
    notice_screens_are_views,
    panel_history,
    view_family,
)
from app.db.models import BotUser
from app.services.notice_links import NOTICE_CLOSE_CALLBACK
from tests.chat_sim import ChatSim

ADMIN_PANEL = "🛠 پنل ادمین"
BACK = "بازگشت"
KEY = (1, 1, 1)


class ViewScreenTests(unittest.TestCase):
    def test_actions_are_never_recorded(self) -> None:
        for data in (
            "adm:users:tagset:5:red", "adm:users:block:5", "adm:users:delask:5", "adm:users:del:5",
            "adm:users:svcdel:5:9", "adm:users:svcrenew:5:9", "adm:users:wcredit:5", "adm:users:msg:5",
            "payrev:ok:3", "ordrev:no:3", "payvoid:ask:3", "payvoid:go:3:1:0", "cnrv:ok:4:1", "cnrv:r:4",
            "autorv:go:2:1", "stk:r:7", "stk:x:rf:7", "tkt:close:1", "nv:adm:close", "nv:cancel:abc",
            "menu:home", "nto:res:user:4", NOTICE_CLOSE_CALLBACK,
        ):
            with self.subTest(data=data):
                self.assertIsNone(view_family(data))

    def test_pages_and_cards_of_one_kind_share_a_family(self) -> None:
        self.assertEqual(view_family("adm:users:list:0"), view_family("adm:users:list:3"))
        self.assertEqual(view_family("adm:users:view:5"), view_family("adm:users:view:6"))
        self.assertNotEqual(view_family("adm:users:svc:5:9"), view_family("adm:users:svcs:5"))
        self.assertNotEqual(view_family("res:user:5"), view_family("res:users:0"))

    def test_every_screen_a_notice_opens_is_recorded(self) -> None:
        self.assertTrue(notice_screens_are_views())


class HistoryStoreTests(unittest.TestCase):
    def test_returning_to_a_shown_screen_drops_what_came_after_it(self) -> None:
        histories = PanelHistories()
        for data in ("nv:adm:users", "adm:users:list:1", "adm:users:view:5", "adm:users:svcs:5"):
            history = histories.get(KEY)
            screens = history.before(data, view_family(data)) if history else []
            histories.show(KEY, screens, Shown(data, view_family(data)), exit=None)
        history = histories.get(KEY)
        self.assertEqual(
            [s.data for s in history.before("adm:users:view:9", view_family("adm:users:view:9"))],
            ["nv:adm:users", "adm:users:list:1"],
        )

    def test_opening_a_screens_fixed_parent_replaces_that_screen(self) -> None:
        histories = PanelHistories()
        histories.show(KEY, [], Shown("stk:v:9", "stk:v:", {"stk:l:0"}), exit=None)
        histories.show(KEY, histories.get(KEY).shown, Shown("adm:users:view:5", "adm:users:view:", {"adm:users:list:0"}), exit=None)
        before = histories.get(KEY).before("adm:users:list:0", "adm:users:list:")
        self.assertEqual([s.data for s in before], ["stk:v:9"])

    def test_a_long_history_keeps_its_newest_screens_and_no_longer_closes(self) -> None:
        histories = PanelHistories(depth=3)
        histories.open_from_notice(KEY)
        screens: list[Shown] = []
        for page in range(5):
            data = f"stk:v:{page}" if page % 2 else f"adm:users:view:{page}"
            histories.show(KEY, screens, Shown(data, view_family(data)), exit=NOTICE_CLOSE_CALLBACK)
            screens = histories.get(KEY).shown
        self.assertLessEqual(len(histories.get(KEY).shown), 3)
        self.assertIsNone(histories.get(KEY).exit)

    def test_depth_default_is_bounded(self) -> None:
        self.assertGreater(HISTORY_DEPTH, 3)


class PlatformHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        for target, value in (
            ("app.bot.middlewares._rate_limited", MagicMock(return_value=False)),
            ("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sim = ChatSim(5301)
        await self.sim.start(owner=True)
        self.addAsyncCleanup(self.sim.stop)
        async with self.sim.Session() as session:
            session.add_all([
                BotUser(id=100 + n, telegram_id=8000000100 + n, referral_code=f"h{n}", full_name=f"Customer {n:02d}")
                for n in range(15)
            ])
            await session.commit()
        await self.sim.type("/start")
        await self.sim.type(ADMIN_PANEL)
        self.panel = [m for m in self.sim.telegram.visible() if m.from_bot and m.inline][-1]

    async def tap_data(self, data: str) -> None:
        await self.sim._tap(self.sim.telegram.messages[self.panel.message_id], data)

    def backs(self) -> list[str]:
        shown = self.sim.telegram.messages[self.panel.message_id]
        return [data for row in shown.inline for text, data in row if BACK in text]

    def data_starting(self, prefix: str) -> str:
        shown = self.sim.telegram.messages[self.panel.message_id]
        return next(data for row in shown.inline for _, data in row if data.startswith(prefix))

    async def open_card_from_second_page(self) -> str:
        await self.tap_data("nv:adm:users")
        await self.tap_data("adm:users:list:0")
        await self.tap_data("adm:users:list:1")
        card = self.data_starting("adm:users:view:")
        await self.tap_data(card)
        return card

    async def test_the_card_goes_back_to_the_page_it_was_opened_from(self) -> None:
        await self.open_card_from_second_page()
        self.assertEqual(self.backs(), ["adm:users:list:1"])

        await self.tap_data("adm:users:list:1")
        self.assertIn("صفحه ۲", self.sim.telegram.messages[self.panel.message_id].text.replace("2", "۲"))

    async def test_another_page_replaces_the_page_instead_of_stacking(self) -> None:
        await self.open_card_from_second_page()
        await self.tap_data("adm:users:list:1")
        self.assertEqual(self.backs(), ["nv:adm:users"])

    async def test_after_an_action_the_card_still_goes_back_to_its_page(self) -> None:
        card = await self.open_card_from_second_page()
        user_id = card.rsplit(":", 1)[-1]
        await self.tap_data(f"adm:users:tag:{user_id}")
        self.assertEqual(self.backs(), [card])
        await self.tap_data(self.data_starting("adm:users:tagset:"))

        self.assertEqual(self.backs(), ["adm:users:list:1"])
        self.assertNotIn("tagset", " ".join(self.backs()))

    async def test_a_typed_reply_shown_in_the_panel_falls_back_to_the_fixed_parent(self) -> None:
        card = await self.open_card_from_second_page()
        user_id = card.rsplit(":", 1)[-1]
        await self.tap_data(f"adm:users:msg:{user_id}")
        await self.sim.type("سلام")
        # The typed reply showed the card again; what the panel showed before it is no longer known.
        await self.tap_data(f"adm:users:tag:{user_id}")
        await self.tap_data(card)

        self.assertEqual(self.backs(), ["adm:users:list:0"])


class ShopHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sim = ChatSim(5401)
        await self.sim.start_shop(staff=True)
        self.addAsyncCleanup(self.sim.stop)
        patcher = patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        async with self.sim.Session() as session:
            session.add_all([
                BotUser(id=200 + n, telegram_id=8100000200 + n, referral_code=f"s{n}", full_name=f"Shopper {n:02d}",
                        reseller_id=900)
                for n in range(12)
            ])
            await session.commit()
        await self.sim.type("/start")

    async def test_a_shop_customer_card_goes_back_to_its_page(self) -> None:
        panel = [m for m in self.sim.telegram.visible() if m.from_bot][-1]
        await self.sim._tap(panel, "res:users:0")
        panel = [m for m in self.sim.telegram.visible() if m.from_bot and m.inline][-1]
        page_two = next((d for row in panel.inline for _, d in row if d.startswith("res:users:") and d != "res:users:0"), None)
        if page_two is None:
            self.skipTest("the shop customers list shows every customer on one page")
        await self.sim._tap(self.sim.telegram.messages[panel.message_id], page_two)
        shown = self.sim.telegram.messages[panel.message_id]
        card = next(d for row in shown.inline for _, d in row if d.startswith("res:user:"))
        await self.sim._tap(shown, card)

        shown = self.sim.telegram.messages[panel.message_id]
        self.assertEqual([d for row in shown.inline for t, d in row if BACK in t], [page_two])


if __name__ == "__main__":
    unittest.main()
