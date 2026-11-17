"""Every ready-made button under a group message opens its screen in a panel under the message, and a plan button
may only open one of the sender's own sellable plans."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.panel_history import panel_history
from app.db.models import Base, BotUser, Plan
from app.services.message_buttons import (
    KIND_PLAN,
    SCREENS,
    ButtonInput,
    ButtonRules,
    build_keyboard,
    dump_buttons,
    load_buttons,
    make_button,
    sellable_plan_ids,
    stored_keyboard,
    target_groups,
)
from app.services.notice_links import is_notice_screen
from app.services.notifications import _send_to_chats
from tests.chat_sim import ChatSim

MESSAGE = "📢 پیام گروهی"


class TargetListTests(unittest.TestCase):
    def test_every_screen_opens_in_a_panel_under_the_message(self) -> None:
        for key, (callback, _label, _group) in SCREENS.items():
            with self.subTest(target=key):
                self.assertTrue(is_notice_screen(callback))
                data = build_keyboard([make_button(ButtonInput(key), ButtonRules())])[0][0].callback_data
                self.assertEqual(data, f"nto:{callback}")

    def test_the_editor_groups_list_every_target_once(self) -> None:
        groups = target_groups(links=True, plans=True, renew=True)
        values = [value for _group, options in groups for value, _label in options]
        self.assertEqual(len(values), len(set(values)))
        self.assertLessEqual(set(SCREENS), set(values))
        self.assertTrue({"plan", "link", "renew"} <= set(values))
        no_extras = [v for _g, options in target_groups(links=False, plans=False) for v, _l in options]
        self.assertFalse({"plan", "link", "renew"} & set(no_extras))


class PlanButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'plans.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=50, telegram_id=50, referral_code="s50", role="reseller"),
            BotUser(id=60, telegram_id=60, referral_code="s60", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            Plan(id=1, name="platform", price=10),
            Plan(id=2, name="shop", price=10, owner_reseller_id=50),
            Plan(id=3, name="other shop", price=10, owner_reseller_id=60),
            Plan(id=4, name="shop trial", price=0, is_trial=True, owner_reseller_id=50),
            Plan(id=5, name="shop off", price=10, owner_reseller_id=50, is_active=False),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_a_sender_may_offer_only_its_own_active_paid_plans(self) -> None:
        self.assertEqual(await sellable_plan_ids(self.session, 50), frozenset({2}))
        self.assertEqual(await sellable_plan_ids(self.session, None), frozenset({1}))
        rules = ButtonRules(plan_ids=await sellable_plan_ids(self.session, 50))
        for plan_id in ("1", "3", "4", "5", "", "abc", "-2"):
            with self.subTest(plan=plan_id), self.assertRaises(ValueError):
                make_button(ButtonInput("plan", plan=plan_id), rules)
        spec = make_button(ButtonInput("plan", plan="2"), rules)
        self.assertEqual((spec.kind, spec.target), (KIND_PLAN, "2"))
        self.assertEqual(build_keyboard([spec])[0][0].callback_data, "nto:shop:plan:2")

    async def test_a_plan_no_longer_sold_is_dropped_from_a_queued_message(self) -> None:
        stored = dump_buttons([make_button(ButtonInput("plan", plan="2"), ButtonRules(plan_ids=frozenset({2})))])
        self.assertEqual(len(await stored_keyboard(self.session, stored, shop_id=50)), 1)
        (await self.session.get(Plan, 2)).is_active = False
        await self.session.commit()
        self.assertEqual(await stored_keyboard(self.session, stored, shop_id=50), [])
        # Another shop can never send it, even from a stored row.
        self.assertEqual(load_buttons(stored, ButtonRules(plan_ids=frozenset({3}))), [])


class ScreenFlowTests(unittest.IsolatedAsyncioTestCase):
    """The customer taps each button under a real message; the screen opens below and the message stays."""

    OPENS = ("services", "wallet", "topup", "support", "invite", "guide", "faq", "shop", "club", "wheel")

    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sim = ChatSim(5301, name="Customer")
        await self.sim.start(owner=False)
        self.addAsyncCleanup(self.sim.stop)

    async def open(self, target: str) -> tuple[int, list]:
        markup = InlineKeyboardMarkup(inline_keyboard=build_keyboard([make_button(ButtonInput(target), ButtonRules())]))
        await _send_to_chats(self.sim.bot, [self.sim.telegram_id], MESSAGE, markup=markup)
        message_id = max(self.sim.telegram.messages)
        await self.sim.tap(SCREENS[target][1])
        panels = [m for m in self.sim.telegram.visible() if m.from_bot and m.message_id > message_id]
        return message_id, panels

    async def test_each_screen_opens_in_its_own_panel_and_the_message_stays(self) -> None:
        for target in self.OPENS:
            with self.subTest(target=target):
                message_id, panels = await self.open(target)
                message = self.sim.telegram.messages[message_id]
                self.assertEqual(message.text, MESSAGE)
                self.assertFalse(message.deleted)
                self.assertEqual(len(panels), 1, self.sim.screen())
                self.assertNotEqual(panels[0].text, "⏳ در حال باز کردن…")

    async def test_a_screen_that_is_off_leaves_no_empty_panel(self) -> None:
        # No trial plan and no custom plan exist: the customer gets an alert and no half-open panel.
        for target in ("trial", "custom"):
            with self.subTest(target=target):
                alerts = len(self.sim.telegram.alerts)
                message_id, panels = await self.open(target)
                self.assertEqual(panels, [], self.sim.screen())
                self.assertGreater(len(self.sim.telegram.alerts), alerts)
                self.assertFalse(self.sim.telegram.messages[message_id].deleted)


if __name__ == "__main__":
    unittest.main()
