"""Command center quick switches: target-value writes, shop isolation, audit and undo, alerts and the panel."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.command_panel import PanelData, needs_confirmation, panel_rows, panel_text
from app.bot.handlers import admin_settings, command_switches, reseller_settings
from app.db import Base
from app.db.models import Plan, ShopStaffAction
from app.services import bot_feedback_texts as feedback
from app.services.admin_counters import PendingCounts
from app.services.quick_switches import (
    OWNER_SWITCHES,
    SHOP_SWITCHES,
    SwitchChange,
    recent_switch_changes,
    set_switch,
    switch_alerts,
    switch_by_code,
    undo_switch_change,
)
from app.services.staff_audit import StaffActor
from app.services.users import clear_settings_cache, get_setting, set_setting
from tests.postgres_support import add_shop_users, scoped_async_engine

SHOP = 21
OTHER_SHOP = 22
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
CARD = switch_by_code(None, "pc")
TRIAL = switch_by_code(None, "tr")
MAINTENANCE = switch_by_code(None, "mt")
ALL_KEYS = frozenset(s.key for s in OWNER_SWITCHES)


def _actor(shop_id: int | None, label: str = "مدیر") -> StaffActor:
    return StaffActor(shop_id=shop_id, kind="platform_owner" if shop_id is None else "shop_owner", user_id=1, label=label)


class SwitchDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.db.models  # noqa: F401  (registers every table)

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        await add_shop_users(self.Session, SHOP, OTHER_SHOP)
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()

    async def _records(self, session) -> int:
        return int(await session.scalar(select(func.count()).select_from(ShopStaffAction)))


class SetSwitchTests(SwitchDb):
    async def test_a_double_tap_sets_the_value_once_and_logs_once(self) -> None:
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1")
            self.assertTrue(await set_switch(session, switch=CARD, shop_id=None, enabled=False, actor=_actor(None)))
            self.assertFalse(await set_switch(session, switch=CARD, shop_id=None, enabled=False, actor=_actor(None)))
            self.assertEqual(await get_setting(session, CARD.key), "0")
            self.assertEqual(await self._records(session), 1)

    async def test_a_shop_switch_changes_only_that_shop(self) -> None:
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1")
            await set_setting(session, CARD.key, "1", reseller_id=OTHER_SHOP)
            await set_switch(session, switch=CARD, shop_id=SHOP, enabled=False, actor=_actor(SHOP))
            self.assertEqual(await get_setting(session, CARD.key, reseller_id=SHOP), "0")
            self.assertEqual(await get_setting(session, CARD.key, reseller_id=OTHER_SHOP), "1")
            self.assertEqual(await get_setting(session, CARD.key), "1")
            self.assertEqual([c.switch.key for c in await recent_switch_changes(session, shop_id=OTHER_SHOP)], [])

    async def test_staff_of_one_shop_cannot_switch_another_shop(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(PermissionError):
                await set_switch(session, switch=CARD, shop_id=OTHER_SHOP, enabled=False, actor=_actor(SHOP))
            with self.assertRaises(PermissionError):
                await set_switch(session, switch=CARD, shop_id=None, enabled=False, actor=_actor(SHOP))
            self.assertEqual(await self._records(session), 0)

    async def test_a_shop_cannot_use_the_platform_trial_switch(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(PermissionError):
                await set_switch(session, switch=TRIAL, shop_id=SHOP, enabled=False, actor=_actor(SHOP))

    async def test_the_trial_switch_stops_selling_the_trial_plan(self) -> None:
        async with self.Session() as session:
            session.add(Plan(name="تست", price=0, is_trial=True, is_active=True))
            await session.commit()
            await set_setting(session, TRIAL.key, "1")
            await set_switch(session, switch=TRIAL, shop_id=None, enabled=False, actor=_actor(None))
            plan = await session.scalar(select(Plan).where(Plan.is_trial.is_(True)))
            self.assertFalse(plan.is_active)


class UndoTests(SwitchDb):
    async def _change_card_off(self, session, shop_id: int | None) -> int:
        await set_setting(session, CARD.key, "1", reseller_id=shop_id)
        await set_switch(session, switch=CARD, shop_id=shop_id, enabled=False, actor=_actor(shop_id))
        return (await recent_switch_changes(session, shop_id=shop_id))[0].record_id

    async def test_undo_puts_the_old_value_back_once(self) -> None:
        async with self.Session() as session:
            record_id = await self._change_card_off(session, None)
            kwargs = {"shop_id": None, "record_id": record_id, "actor": _actor(None), "allowed_keys": ALL_KEYS}
            self.assertTrue(await undo_switch_change(session, **kwargs))
            self.assertEqual(await get_setting(session, CARD.key), "1")
            self.assertFalse(await undo_switch_change(session, **kwargs))

    async def test_undo_never_overwrites_a_later_change(self) -> None:
        async with self.Session() as session:
            record_id = await self._change_card_off(session, None)
            await set_setting(session, CARD.key, "1")
            undone = await undo_switch_change(
                session, shop_id=None, record_id=record_id, actor=_actor(None), allowed_keys=ALL_KEYS
            )
            self.assertFalse(undone)

    async def test_another_shops_record_cannot_be_undone(self) -> None:
        async with self.Session() as session:
            record_id = await self._change_card_off(session, OTHER_SHOP)
            undone = await undo_switch_change(
                session, shop_id=SHOP, record_id=record_id, actor=_actor(SHOP), allowed_keys=ALL_KEYS
            )
            self.assertFalse(undone)
            self.assertEqual(await get_setting(session, CARD.key, reseller_id=OTHER_SHOP), "0")

    async def test_a_switch_the_actor_may_not_change_cannot_be_undone(self) -> None:
        async with self.Session() as session:
            record_id = await self._change_card_off(session, SHOP)
            undone = await undo_switch_change(
                session, shop_id=SHOP, record_id=record_id, actor=_actor(SHOP), allowed_keys=frozenset({"x"})
            )
            self.assertFalse(undone)


class AlertTests(unittest.TestCase):
    def _change(self, key_code: str, enabled: bool, at: datetime) -> SwitchChange:
        return SwitchChange(1, switch_by_code(None, key_code), enabled, "مدیر", at)

    def test_no_payment_method_and_maintenance_are_reported(self) -> None:
        alerts = switch_alerts({"shop_maintenance_enabled": "1"}, [], shop_id=SHOP, now=NOW)
        self.assertEqual(len(alerts), 2)
        self.assertIn("پرداخت", alerts[0])

    def test_a_trial_left_off_for_days_is_reported_to_the_owner_only(self) -> None:
        ui = {"pay_card_enabled": "1", "trial_enabled": "0"}
        changes = [self._change("tr", False, NOW - timedelta(days=3))]
        self.assertIn("۳", switch_alerts(ui, changes, shop_id=None, now=NOW)[0])
        self.assertEqual(switch_alerts(ui, [self._change("tr", False, NOW - timedelta(hours=2))], shop_id=None, now=NOW), [])

    def test_a_panel_outage_is_shown_on_the_platform_only(self) -> None:
        ui = {"pay_card_enabled": "1", "pg_link_alert_outage_active": "1"}
        self.assertTrue(switch_alerts(ui, [], shop_id=None, now=NOW))
        self.assertEqual(switch_alerts(ui, [], shop_id=SHOP, now=NOW), [])


class PanelTests(unittest.TestCase):
    def _data(self, ui: dict[str, str], changes: list[SwitchChange] | None = None) -> PanelData:
        return PanelData(ui=ui, queue=PendingCounts(), changes=changes or [], now=NOW)

    def _callbacks(self, rows) -> list[str]:
        return [b.callback_data for row in rows for b in row]

    def test_each_switch_sets_the_opposite_of_what_it_shows(self) -> None:
        rows = panel_rows(self._data({"pay_card_enabled": "1"}), OWNER_SWITCHES, cards_callback="adm:st:sub:pay:card")
        callbacks = self._callbacks(rows)
        self.assertIn("cmd:t:pc:0", callbacks)
        self.assertIn("cmd:t:tr:1", callbacks)
        self.assertTrue(all(len(c.encode()) <= 64 for c in callbacks))

    def test_undo_is_offered_only_while_the_change_still_holds_and_for_allowed_switches(self) -> None:
        change = SwitchChange(9, CARD, False, "مدیر", NOW)
        self.assertIn("cmd:u:9", self._callbacks(panel_rows(self._data({}, [change]), OWNER_SWITCHES, cards_callback=None)))
        held_back = self._data({"pay_card_enabled": "1"}, [change])
        self.assertNotIn("cmd:u:9", self._callbacks(panel_rows(held_back, OWNER_SWITCHES, cards_callback=None)))
        self.assertNotIn("cmd:u:9", self._callbacks(panel_rows(self._data({}, [change]), (MAINTENANCE,), cards_callback=None)))

    def test_staff_names_are_escaped(self) -> None:
        change = SwitchChange(9, CARD, False, "<b>Ali</b>", NOW)
        text = panel_text(self._data({}, [change]), shop_id=None, can_switch=True)
        self.assertIn("&lt;b&gt;Ali", text)

    def test_closing_the_shop_or_its_last_way_to_pay_asks_first(self) -> None:
        self.assertIsNotNone(needs_confirmation(MAINTENANCE, True, {}))
        self.assertIsNone(needs_confirmation(MAINTENANCE, False, {}))
        self.assertIsNotNone(needs_confirmation(CARD, False, {"pay_card_enabled": "1"}))
        self.assertIsNone(needs_confirmation(CARD, False, {"pay_card_enabled": "1", "pay_wallet_enabled": "1"}))


class CatalogueConsistencyTests(unittest.TestCase):
    def test_every_switch_is_a_toggle_its_settings_screen_already_offers(self) -> None:
        for switch in OWNER_SWITCHES:
            self.assertTrue(admin_settings._is_toggleable_setting_key(switch.key), switch.key)
        for switch in SHOP_SWITCHES:
            self.assertEqual(reseller_settings.FIELDS[switch.key][2], "toggle", switch.key)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, data: str) -> MagicMock:
        callback = MagicMock()
        callback.data = data
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        return callback

    def _viewer(self, *, switches=SHOP_SWITCHES, actor=True) -> command_switches.SwitchViewer:
        return command_switches.SwitchViewer(
            SHOP, "nv:res:home", switches, _actor(SHOP) if actor else None, "res:st:sub:pay:card"
        )

    async def _toggle(self, data: str, viewer, **patches):
        callback = self._callback(data)
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
        with patch.object(command_switches, "load_switch_viewer", AsyncMock(return_value=viewer)), patch.object(
            command_switches, "set_switch_by_hand", AsyncMock(return_value=True)
        ) as setter, patch.object(command_switches, "show_command_panel", AsyncMock()), patch.object(
            command_switches, "get_all_settings", AsyncMock(return_value=patches.get("ui", {}))
        ):
            await command_switches.toggle_quick_switch(callback, MagicMock(), SimpleNamespace(id=5), state, is_reseller_bot=True)
        return callback, setter

    async def test_someone_without_panel_access_is_refused(self) -> None:
        callback, setter = await self._toggle("cmd:t:pc:0", None)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        setter.assert_not_awaited()

    async def test_shop_staff_without_settings_access_cannot_switch(self) -> None:
        callback, setter = await self._toggle("cmd:t:pc:0", self._viewer(switches=(), actor=False))
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        setter.assert_not_awaited()

    async def test_a_switch_outside_the_viewers_package_is_refused(self) -> None:
        without_approval = tuple(s for s in SHOP_SWITCHES if s.code != "ea")
        callback, setter = await self._toggle("cmd:t:ea:1", self._viewer(switches=without_approval))
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        setter.assert_not_awaited()

    async def test_malformed_callback_data_is_rejected(self) -> None:
        for data in ("cmd:t:pc:2", "cmd:t:pc", "cmd:t:pc:1:z"):
            callback, setter = await self._toggle(data, self._viewer())
            callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)
            setter.assert_not_awaited()

    async def test_maintenance_asks_first_and_switches_only_after_yes(self) -> None:
        callback, setter = await self._toggle("cmd:t:mt:1", self._viewer())
        setter.assert_not_awaited()
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "cmd:t:mt:1:y")
        _callback, setter = await self._toggle("cmd:t:mt:1:y", self._viewer())
        self.assertEqual(setter.await_args.kwargs["enabled"], True)
        self.assertEqual(setter.await_args.kwargs["shop_id"], SHOP)


if __name__ == "__main__":
    unittest.main()
