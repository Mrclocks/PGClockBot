"""The menu editor renames and recolors the shop's own categories and plans, and nobody else's."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.edit_mode import CATALOG_ROW_SCREENS
from app.bot.handlers.catalog_row_editor import parse_request
from app.bot.handlers.screen_editor import BTN_CATALOG_ROWS, panel_view
from app.bot.panel_history import panel_history
from app.bot.screen_registry import SCREENS, ScreenContext, screen_buttons
from app.db.models import Base, BotUser, Plan, PlanCategory
from app.services import catalog_row_edits as rows
from app.services.screen_editor import EditorActor, edit_mode_store
from app.services.shop_scope import ShopScopeError
from tests.chat_sim import ChatSim

OWNER_STAFF = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "org_status": "active"}
SHOP_A, SHOP_B = 900, 901


def _shop_staff(shop_id: int) -> dict:
    return {"role": "reseller", "bot_user_id": shop_id}


class _Catalog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.engine.dispose)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=9000, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=9001, referral_code="b", role="reseller"),
                PlanCategory(id=1, name="Platform cat", owner_reseller_id=None, audience="users"),
                PlanCategory(id=2, name="Shop A cat", owner_reseller_id=SHOP_A, audience="users",
                             icon_custom_emoji_id="555"),
                PlanCategory(id=3, name="Shop B cat", owner_reseller_id=SHOP_B, audience="users"),
                PlanCategory(id=4, name="Agency label", owner_reseller_id=None, audience="resellers"),
                Plan(id=10, name="Platform plan", price=1000, owner_reseller_id=None, is_active=True),
                Plan(id=11, name="Shop A plan", price=1000, owner_reseller_id=SHOP_A, is_active=True),
                Plan(id=12, name="Shop B plan", price=1000, owner_reseller_id=SHOP_B, is_active=True),
                Plan(id=13, name="Trial", price=0, owner_reseller_id=SHOP_A, is_active=True, is_trial=True),
                Plan(id=14, name="Old", price=1000, owner_reseller_id=SHOP_A, is_active=False),
            ])
            await session.commit()


class ScopeTests(_Catalog):
    async def test_each_shop_lists_only_its_own_customer_rows(self) -> None:
        async with self.Session() as session:
            shop = await rows.editable_rows(session, _shop_staff(SHOP_A))
            platform = await rows.editable_rows(session, OWNER_STAFF)
        self.assertEqual([c.id for c in shop.categories], [2])
        self.assertEqual([p.id for p in shop.plans], [11])
        self.assertEqual([c.id for c in platform.categories], [1])
        self.assertEqual([p.id for p in platform.plans], [10])

    async def test_another_shops_rows_cannot_be_changed(self) -> None:
        changes = (
            lambda s: rows.rename_category(s, _shop_staff(SHOP_A), 3, name="x", icon_custom_emoji_id=None),
            lambda s: rows.set_category_color(s, _shop_staff(SHOP_A), 3, "d"),
            lambda s: rows.clear_category_icon(s, _shop_staff(SHOP_A), 3),
            lambda s: rows.rename_plan(s, _shop_staff(SHOP_A), 12, name="x"),
            lambda s: rows.set_plan_color(s, _shop_staff(SHOP_A), 12, "d"),
            lambda s: rows.rename_plan(s, _shop_staff(SHOP_A), 10, name="x"),
            lambda s: rows.rename_plan(s, OWNER_STAFF, 11, name="x"),
            lambda s: rows.set_category_color(s, OWNER_STAFF, 2, "d"),
            lambda s: rows.rename_category(s, OWNER_STAFF, 4, name="x", icon_custom_emoji_id=None),
        )
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                async with self.Session() as session:
                    with self.assertRaises(ShopScopeError):
                        await change(session)
        async with self.Session() as session:
            self.assertEqual((await session.get(PlanCategory, 3)).name, "Shop B cat")
            self.assertEqual((await session.get(Plan, 12)).name, "Shop B plan")
            self.assertIsNone((await session.get(Plan, 11)).button_style)

    async def test_a_staff_member_without_a_shop_changes_nothing(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ShopScopeError):
                await rows.rename_plan(session, {"role": "reseller"}, 11, name="x")


class ChangeTests(_Catalog):
    async def test_a_category_keeps_its_icon_unless_a_new_one_is_sent(self) -> None:
        async with self.Session() as session:
            renamed = await rows.rename_category(session, _shop_staff(SHOP_A), 2, name="  VIP ", icon_custom_emoji_id=None)
            self.assertEqual((renamed.name, renamed.icon_custom_emoji_id), ("VIP", "555"))
            renamed = await rows.rename_category(session, _shop_staff(SHOP_A), 2, name="VIP", icon_custom_emoji_id="777")
            self.assertEqual(renamed.icon_custom_emoji_id, "777")
            cleared = await rows.clear_category_icon(session, _shop_staff(SHOP_A), 2)
            self.assertIsNone(cleared.icon_custom_emoji_id)

    async def test_colors_are_saved_and_inherit_clears_them(self) -> None:
        async with self.Session() as session:
            self.assertEqual((await rows.set_category_color(session, _shop_staff(SHOP_A), 2, "s")).button_style, "success")
            self.assertIsNone((await rows.set_category_color(session, _shop_staff(SHOP_A), 2, "i")).button_style)
            self.assertEqual((await rows.set_plan_color(session, _shop_staff(SHOP_A), 11, "w")).button_style, "")
            self.assertIsNone((await rows.set_plan_color(session, _shop_staff(SHOP_A), 11, "i")).button_style)

    async def test_bad_input_is_refused(self) -> None:
        async with self.Session() as session:
            for name in ("", "   ", "x" * (rows.NAME_MAX_LENGTH + 1)):
                with self.subTest(name=name[:5]), self.assertRaises(ValueError):
                    await rows.rename_plan(session, _shop_staff(SHOP_A), 11, name=name)
            with self.assertRaises(ValueError):
                await rows.set_plan_color(session, _shop_staff(SHOP_A), 11, "z")
            with self.assertRaises(ValueError):
                await rows.rename_category(session, _shop_staff(SHOP_A), 2, name=" ", icon_custom_emoji_id=None)
            self.assertEqual((await session.get(Plan, 11)).name, "Shop A plan")


class RequestTests(unittest.TestCase):
    def test_only_well_formed_requests_are_accepted(self) -> None:
        self.assertEqual(parse_request("edr:a:l").view, "l")
        self.assertEqual(parse_request("edr:r:pc:11:s").color, "s")
        for data in ("edr:a:c", "edr:a:l:3", "edr:a:cc:3", "edr:a:c:3:s", "edr:x:l", "edr:a:pc:1:z", "ed:a:l", None):
            with self.subTest(data=data):
                self.assertIsNone(parse_request(data))

    def test_the_shop_screens_link_to_the_rows(self) -> None:
        actor = EditorActor(user_id=1, shop_id=None)
        for screen_id in CATALOG_ROW_SCREENS:
            with self.subTest(screen=screen_id):
                spec = SCREENS[screen_id]
                _, markup = panel_view(actor, spec, screen_buttons(screen_id, ScreenContext(ui={})), {}, minutes_left=5)
                links = [b.callback_data for row in markup.inline_keyboard for b in row if b.text == BTN_CATALOG_ROWS]
                self.assertEqual(links, ["edr:a:l"])
        _, markup = panel_view(actor, SCREENS["c.wallet"], screen_buttons("c.wallet", ScreenContext(ui={})), {}, minutes_left=5)
        self.assertNotIn(BTN_CATALOG_ROWS, [b.text for row in markup.inline_keyboard for b in row])


class EditorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        self.sim = ChatSim(5801)
        await self.sim.start(owner=True)
        self.addAsyncCleanup(self.sim.stop)
        for target, value in (
            ("app.bot.middlewares._rate_limited", MagicMock(return_value=False)),
            ("app.bot.handlers.catalog_row_editor.resolve_catalog_staff", AsyncMock(return_value=OWNER_STAFF)),
            ("app.bot.handlers.catalog_row_editor.is_editor_enabled", AsyncMock(return_value=True)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.sim.Session() as session:
            session.add_all([
                PlanCategory(id=1, name="اقتصادی", owner_reseller_id=None, audience="users"),
                Plan(id=10, name="یک ماهه", price=1000, owner_reseller_id=None, is_active=True),
            ])
            await session.commit()
        await self.sim.type("/start")
        async with self.sim.Session() as session:
            owner = await session.scalar(select(BotUser).where(BotUser.telegram_id == 5801))
        self.actor = EditorActor(user_id=int(owner.id), shop_id=None)
        edit_mode_store.activate(self.actor)
        self.addCleanup(edit_mode_store.deactivate, self.actor)
        self.panel = [m for m in self.sim.telegram.visible() if m.from_bot][-1]

    async def tap(self, data: str) -> None:
        await self.sim._tap(self.sim.telegram.messages[self.panel.message_id], data)

    def shown(self) -> str:
        return self.sim.telegram.messages[self.panel.message_id].text

    async def test_rename_a_category_and_recolor_a_plan(self) -> None:
        await self.tap("edr:a:l")
        self.assertIn("اقتصادی", " ".join(t for row in self.sim.telegram.messages[self.panel.message_id].inline for t, _ in row))
        await self.tap("edr:a:cn:1")
        await self.sim.type("اقتصادی <ویژه>")
        async with self.sim.Session() as session:
            self.assertEqual((await session.get(PlanCategory, 1)).name, "اقتصادی <ویژه>")
        self.assertTrue(any("&lt;ویژه&gt;" in m.text for m in self.sim.telegram.visible() if m.from_bot), self.sim.screen())

        await self.tap("edr:a:pc:10:d")
        async with self.sim.Session() as session:
            self.assertEqual((await session.get(Plan, 10)).button_style, "danger")

    async def test_nothing_changes_once_edit_mode_is_off(self) -> None:
        edit_mode_store.deactivate(self.actor)
        await self.tap("edr:a:pc:10:d")
        async with self.sim.Session() as session:
            self.assertIsNone((await session.get(Plan, 10)).button_style)
        self.assertTrue(self.sim.telegram.alerts)

    async def test_a_cancelled_rename_changes_nothing(self) -> None:
        await self.tap("edr:a:pn:10")
        await self.sim.type("انصراف")
        async with self.sim.Session() as session:
            self.assertEqual((await session.get(Plan, 10)).name, "یک ماهه")


if __name__ == "__main__":
    unittest.main()
