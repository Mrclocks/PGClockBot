"""Shop lists in the menu editor: real categories and plans, their order (web sort_order) and columns."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import keyboards as kb
from app.bot.catalog_panel import BTN_NO_MOVE
from app.bot.edit_mode import editing_as
from app.bot.handlers import catalog_row_editor as rows_editor
from app.bot.handlers import menu_rows_editor as layout_editor
from app.bot.handlers import screen_editor as editor_ui
from app.db.models import Base, BotUser, Plan, PlanCategory, ResellerSetting, Setting
from app.services import bot_feedback_texts as feedback
from app.services.catalog_order import catalog_list
from app.services.menu_rows import columns_setting_key
from app.services.orders import list_active_plans
from app.services.screen_editor import SCREEN_EDITOR_SETTING, EditorActor, edit_mode_store
from app.services.users import clear_settings_cache, get_all_settings
from tests.postgres_support import scoped_async_engine
from tests.test_bot_ui_keyboard_snapshots import default_ui

OWNER = EditorActor(user_id=7, shop_id=None)
SHOP_A, SHOP_B = 910, 911
SHOP_OWNER = EditorActor(user_id=SHOP_A, shop_id=SHOP_A)
OWNER_STAFF = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "org_status": "active"}
SHOP_STAFF = {"role": "reseller", "bot_user_id": SHOP_A}


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _data(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class CatalogEditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=OWNER.user_id, telegram_id=7000, referral_code="o", role="admin"),
                BotUser(id=SHOP_A, telegram_id=9100, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=9101, referral_code="b", role="reseller"),
                PlanCategory(id=1, name="اقتصادی", owner_reseller_id=None, audience="users"),
                PlanCategory(id=2, name="ویژه", owner_reseller_id=None, audience="users"),
                PlanCategory(id=3, name="فروشگاه الف", owner_reseller_id=SHOP_A, audience="users"),
            ])
            await session.commit()
            session.add_all([
                Plan(id=10, name="یک ماهه", price=1000, category_id=1, is_active=True),
                Plan(id=11, name="سه ماهه", price=2000, category_id=1, is_active=True),
                Plan(id=12, name="ویژه ماهانه", price=3000, category_id=2, is_active=True),
                Plan(id=13, name="بی‌دسته", price=500, is_active=True),
                Plan(id=14, name="تست", price=0, is_active=True, is_trial=True),
                Plan(id=20, name="الف یک", price=1000, category_id=3, owner_reseller_id=SHOP_A, is_active=True),
                Plan(id=21, name="الف دو", price=1000, category_id=3, owner_reseller_id=SHOP_A, is_active=True),
                Plan(id=30, name="ب یک", price=1000, owner_reseller_id=SHOP_B, is_active=True),
                Setting(key=SCREEN_EDITOR_SETTING, value="1"),
            ])
            await session.commit()
        clear_settings_cache()
        edit_mode_store.activate(OWNER)
        edit_mode_store.activate(SHOP_OWNER)
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        self.staff = AsyncMock(return_value=OWNER_STAFF)
        patcher = patch.object(rows_editor, "resolve_catalog_staff", self.staff)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        edit_mode_store.deactivate(OWNER)
        edit_mode_store.deactivate(SHOP_OWNER)
        editor_ui._LIST_TOKENS.clear()
        clear_settings_cache()
        await self.engine.dispose()

    async def _panel(self, data: str) -> InlineKeyboardMarkup:
        callback = _callback(data)
        async with self.Session() as session:
            await editor_ui.owner_editor(callback, session, MagicMock(id=OWNER.user_id), self.state)
        return callback.message.edit_text.await_args.kwargs["reply_markup"]

    async def _panel_text(self, data: str) -> str:
        callback = _callback(data)
        async with self.Session() as session:
            await editor_ui.owner_editor(callback, session, MagicMock(id=OWNER.user_id), self.state)
        return callback.message.edit_text.await_args.args[0]

    async def _rows(self, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await rows_editor.owner_rows(callback, session, MagicMock(id=OWNER.user_id), self.state)
        return callback

    async def _shop_rows(self, data: str) -> MagicMock:
        callback = _callback(data)
        with patch.object(editor_ui, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
            async with self.Session() as session:
                await rows_editor.shop_rows(
                    callback, session, MagicMock(id=SHOP_A), self.state, is_reseller_bot=True, reseller_owner_id=SHOP_A
                )
        return callback

    async def _layout(self, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await layout_editor.owner_rows(callback, session, MagicMock(id=OWNER.user_id))
        return callback

    async def _orders(self, *ids: int) -> list[int]:
        async with self.Session() as session:
            rows = (await session.scalars(select(Plan).where(Plan.id.in_(ids)))).all()
        return [p.id for p in sorted(rows, key=lambda p: (p.sort_order, p.id))]

    @staticmethod
    def _alerted(callback: MagicMock) -> bool:
        return bool(callback.answer.await_args and callback.answer.await_args.kwargs.get("show_alert"))

    @staticmethod
    def _move(markup: InlineKeyboardMarkup, index: int, target: int) -> str:
        return next(d for d in _data(markup) if d.startswith("edr:") and ":m:" in d and f":{index}:{target}:" in d)

    # ── Listing ──

    async def test_lists_follow_what_customers_see_in_each_shop(self) -> None:
        async with self.Session() as session:
            names = {
                token: [item.name for item in (await catalog_list(session, shop_id=None, token=token)).items]
                for token in ("k", "c1", "n", "t", "w")
            }
            shop = await catalog_list(session, shop_id=SHOP_A, token="f")
            foreign = await catalog_list(session, shop_id=SHOP_A, token="c1")
        self.assertEqual(names["k"], ["اقتصادی", "ویژه"])
        self.assertEqual(names["c1"], ["یک ماهه", "سه ماهه"])
        self.assertEqual(names["n"], ["بی‌دسته"])
        self.assertEqual(names["t"], ["تست"])
        self.assertEqual(names["w"], ["یک ماهه", "سه ماهه", "ویژه ماهانه", "بی‌دسته"])
        self.assertEqual([item.id for item in shop.items], [20, 21])
        # Another shop's category id lists nothing and does not reveal its name.
        self.assertEqual(foreign.items, ())
        self.assertEqual(foreign.title, "یک دسته")

    async def test_the_edit_button_under_a_plan_list_names_that_list(self) -> None:
        async with self.Session() as session:
            plans = [p for p in await list_active_plans(session) if p.category_id == 1]
        with editing_as(OWNER):
            markup = kb.plans_keyboard(plans, default_ui(), kind="fixed", edit_list="c1")
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "ed:a:o:c.shop.plans:c1")

    async def test_the_panel_shows_the_real_plans_of_that_list(self) -> None:
        text = await self._panel_text("ed:a:o:c.shop.plans:c1")
        markup = await self._panel("ed:a:o:c.shop.plans:c1")
        texts = _texts(markup)
        self.assertIn("💎 یک ماهه", texts)
        self.assertIn("💎 سه ماهه", texts)
        self.assertNotIn("💎 ویژه ماهانه", texts)
        self.assertNotIn(feedback.NO_PLANS, " ".join(texts))
        self.assertIn("دستهٔ اقتصادی", text)
        self.assertIn("edr:a:p:10", _data(markup))
        first = next(row for row in markup.inline_keyboard if any(b.text == "💎 یک ماهه" for b in row))
        self.assertEqual(first[0].text, BTN_NO_MOVE)
        # Views without the list in their callback come back to the same list.
        self.assertIn("💎 یک ماهه", _texts(await self._panel("ed:a:o:c.shop.plans")))

    async def test_the_shop_panel_lists_the_shop_categories(self) -> None:
        texts = _texts(await self._panel("ed:a:o:c.shop"))
        self.assertIn("🏷 اقتصادی", texts)
        self.assertIn("🏷 ویژه", texts)
        self.assertNotIn("🏷 فروشگاه الف", texts)

    # ── Order ──

    async def test_a_move_rewrites_the_web_order_and_customers_see_it(self) -> None:
        markup = await self._panel("ed:a:o:c.shop.plans:c1")
        callback = await self._rows(self._move(markup, 0, 1))
        self.assertEqual(callback.answer.await_args.args[0], rows_editor.TEXT_MOVED)
        self.assertEqual(await self._orders(10, 11), [11, 10])
        async with self.Session() as session:
            listing = await catalog_list(session, shop_id=None, token="c1")
        self.assertEqual([item.id for item in listing.items], [11, 10])
        panel = callback.message.edit_text.await_args.kwargs["reply_markup"]
        names = [t for t in _texts(panel) if t.startswith("💎")]
        self.assertEqual(names, ["💎 سه ماهه", "💎 یک ماهه"])

    async def test_a_double_tap_moves_one_slot_only(self) -> None:
        data = self._move(await self._panel("ed:a:o:c.shop.plans:c1"), 0, 1)
        await self._rows(data)
        self.assertTrue(self._alerted(await self._rows(data)))
        self.assertEqual(await self._orders(10, 11), [11, 10])

    async def test_moving_categories_changes_the_shop_menu(self) -> None:
        await self._rows(self._move(await self._panel("ed:a:o:c.shop"), 1, 0))
        async with self.Session() as session:
            listing = await catalog_list(session, shop_id=None, token="k")
        self.assertEqual([item.name for item in listing.items], ["ویژه", "اقتصادی"])

    async def test_a_shop_reorders_only_its_own_rows(self) -> None:
        self.staff.return_value = SHOP_STAFF
        async with self.Session() as session:
            listing = await catalog_list(session, shop_id=SHOP_A, token="c3")
        await self._shop_rows(f"edr:r:m:c3:0:1:{listing.state}")
        self.assertEqual(await self._orders(20, 21), [21, 20])
        self.assertEqual(await self._orders(10, 11), [10, 11])
        # A platform list named from the shop bot is the shop's own (empty) list: nothing moves.
        async with self.Session() as session:
            platform = await catalog_list(session, shop_id=None, token="c1")
        self.assertTrue(self._alerted(await self._shop_rows(f"edr:r:m:c1:0:1:{platform.state}")))
        self.assertEqual(await self._orders(10, 11), [10, 11])

    async def test_no_move_without_catalog_permission_or_edit_mode(self) -> None:
        data = self._move(await self._panel("ed:a:o:c.shop.plans:c1"), 0, 1)
        self.staff.return_value = None
        self.assertTrue(self._alerted(await self._rows(data)))
        self.staff.return_value = OWNER_STAFF
        edit_mode_store.deactivate(OWNER)
        self.assertTrue(self._alerted(await self._rows(data)))
        self.assertEqual(await self._orders(10, 11), [10, 11])

    async def test_equal_sort_values_are_spread_so_the_move_shows(self) -> None:
        async with self.Session() as session:
            listing = await catalog_list(session, shop_id=None, token="w")
        await self._rows(f"edr:a:m:w:2:1:{listing.state}")
        self.assertEqual(await self._orders(10, 11, 12, 13), [10, 12, 11, 13])

    def test_forged_moves_do_not_parse(self) -> None:
        for data in ("edr:a:m:x:0:1:abcdef", "edr:a:m:c1:0:1", "edr:a:m:c1:0:1:ABCDEF", "edr:a:m:c1:a:1:abcdef"):
            with self.subTest(data=data):
                self.assertIsNone(rows_editor.parse_move(data))

    # ── Columns ──

    async def test_columns_are_saved_per_list_and_applied(self) -> None:
        markup = await self._panel("ed:a:o:c.shop.plans:c1")
        self.assertIn("edl:a:w:c.shop.plans:1", _data(markup))
        await self._layout("edl:a:w:c.shop.plans:1")
        async with self.Session() as session:
            stored = await session.scalar(select(Setting.value).where(Setting.key == columns_setting_key("c.shop.plans")))
            ui = await get_all_settings(session)
            plans = [p for p in await list_active_plans(session) if not p.is_trial]
        self.assertEqual(stored, "1")
        rows = kb.plans_keyboard(plans, ui, kind="fixed").inline_keyboard
        self.assertTrue(all(len(row) == 1 for row in rows))
        await self._layout("edl:a:w:c.shop.plans:2")
        async with self.Session() as session:
            ui = await get_all_settings(session)
        self.assertEqual([len(r) for r in kb.plans_keyboard(plans, ui, kind="fixed").inline_keyboard[:2]], [2, 2])
        await self._layout("edl:a:w:c.shop.plans:0")
        async with self.Session() as session:
            stored = await session.scalar(select(Setting.value).where(Setting.key == columns_setting_key("c.shop.plans")))
        self.assertEqual(stored, "")

    async def test_shop_columns_are_the_shop_own(self) -> None:
        callback = _callback("edl:r:w:c.shop:1")
        with patch.object(editor_ui, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
            async with self.Session() as session:
                await layout_editor.shop_rows(
                    callback, session, MagicMock(id=SHOP_A), is_reseller_bot=True, reseller_owner_id=SHOP_A
                )
        async with self.Session() as session:
            shop = await session.scalar(select(ResellerSetting.value).where(
                ResellerSetting.reseller_user_id == SHOP_A, ResellerSetting.key == columns_setting_key("c.shop")))
            platform = await session.scalar(select(Setting.value).where(Setting.key == columns_setting_key("c.shop")))
        self.assertEqual(shop, "1")
        self.assertIsNone(platform)

    def test_column_callbacks_are_checked(self) -> None:
        for data in ("edl:a:w:c.wallet:1", "edl:a:w:c.shop:3", "edl:a:w:c.shop"):
            with self.subTest(data=data):
                self.assertIsNone(layout_editor.parse_request(data))


if __name__ == "__main__":
    unittest.main()
