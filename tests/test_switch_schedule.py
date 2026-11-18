"""Timed quick switches and scenarios: one revert per change, applied once, never over a later change."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import command_schedules, command_switches
from app.db import Base
from app.db.models import SwitchRevert
from app.services import bot_feedback_texts as feedback
from app.services.quick_switches import SHOP_SWITCHES, switch_by_code
from app.services.staff_audit import StaffActor
from app.services.switch_schedule import (
    apply_revert,
    change_for_a_while,
    end_scenario,
    next_morning,
    run_due_reverts,
    run_scenario,
    scenario_by_code,
)
from app.services.users import clear_settings_cache, get_setting, set_setting

ROOT = Path(__file__).resolve().parents[1]
SHOP = 21
OTHER_SHOP = 22
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
CARD = switch_by_code(None, "pc")
SHOP_KEYS = frozenset(s.key for s in SHOP_SWITCHES)


def _actor(shop_id: int | None) -> StaffActor:
    return StaffActor(shop_id=shop_id, kind="shop_owner", user_id=1, label="مدیر")


class ScheduleDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.db.models  # noqa: F401  (registers every table)

        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'schedule.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _card(self, shop_id: int | None) -> str:
        clear_settings_cache()
        async with self.Session() as session:
            return await get_setting(session, CARD.key, reseller_id=shop_id)

    async def _statuses(self) -> list[str]:
        async with self.Session() as session:
            return list(await session.scalars(select(SwitchRevert.status).order_by(SwitchRevert.id)))

    async def _card_off_for_an_hour(self, shop_id: int | None = SHOP) -> None:
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1", reseller_id=shop_id)
            await change_for_a_while(
                session, switch=CARD, shop_id=shop_id, enabled=False, actor=_actor(shop_id),
                revert_at=NOW + timedelta(hours=1),
            )


class TimedSwitchTests(ScheduleDb):
    async def test_the_switch_comes_back_once_its_time_comes(self) -> None:
        await self._card_off_for_an_hour()
        self.assertEqual(await self._card(SHOP), "0")
        self.assertEqual(await run_due_reverts(self.Session, now=NOW), 0)
        self.assertEqual(await run_due_reverts(self.Session, now=NOW + timedelta(hours=2)), 1)
        self.assertEqual(await self._card(SHOP), "1")
        self.assertEqual(await run_due_reverts(self.Session, now=NOW + timedelta(hours=3)), 0)
        self.assertEqual(await self._statuses(), ["done"])

    async def test_a_later_change_by_a_person_is_never_overwritten(self) -> None:
        await self._card_off_for_an_hour()
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1", reseller_id=SHOP)
            await set_setting(session, CARD.key, "0", reseller_id=SHOP)
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1", reseller_id=SHOP)
        self.assertEqual(await run_due_reverts(self.Session, now=NOW + timedelta(hours=2)), 0)
        self.assertEqual(await self._statuses(), ["skipped"])

    async def test_a_new_timer_replaces_the_pending_one(self) -> None:
        await self._card_off_for_an_hour()
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1", reseller_id=SHOP)
            await change_for_a_while(
                session, switch=CARD, shop_id=SHOP, enabled=False, actor=_actor(SHOP), revert_at=NOW
            )
        self.assertEqual(await self._statuses(), ["cancelled", "pending"])

    async def test_a_switch_already_in_that_state_schedules_nothing(self) -> None:
        async with self.Session() as session:
            await set_setting(session, CARD.key, "0", reseller_id=SHOP)
            changed = await change_for_a_while(
                session, switch=CARD, shop_id=SHOP, enabled=False, actor=_actor(SHOP), revert_at=NOW
            )
        self.assertFalse(changed)
        self.assertEqual(await self._statuses(), [])

    async def test_a_revert_of_another_shop_or_of_a_forbidden_switch_is_refused(self) -> None:
        await self._card_off_for_an_hour(OTHER_SHOP)
        async with self.Session() as session:
            revert_id = await session.scalar(select(SwitchRevert.id))
            self.assertFalse(await apply_revert(session, revert_id, shop_id=SHOP, actor=_actor(SHOP)))
            self.assertFalse(
                await apply_revert(
                    session, revert_id, shop_id=OTHER_SHOP, actor=_actor(OTHER_SHOP), allowed_keys=frozenset({"x"})
                )
            )
        self.assertEqual(await self._card(OTHER_SHOP), "0")


class ScenarioTests(ScheduleDb):
    async def test_a_shop_runs_only_the_actions_it_may_take_and_ends_them(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "shop_maintenance_enabled", "0", reseller_id=SHOP)
            changed = await run_scenario(
                session, scenario=scenario_by_code("down"), shop_id=SHOP, allowed=SHOP_SWITCHES,
                actor=_actor(SHOP), now=NOW,
            )
        self.assertEqual([s.code for s in changed], ["mt"])  # shops have no trial switch
        async with self.Session() as session:
            restored = await end_scenario(
                session, shop_id=OTHER_SHOP, scenario="down", actor=_actor(OTHER_SHOP), allowed_keys=SHOP_KEYS
            )
            self.assertEqual(restored, 0)
            restored = await end_scenario(
                session, shop_id=SHOP, scenario="down", actor=_actor(SHOP), allowed_keys=SHOP_KEYS
            )
        self.assertEqual(restored, 1)
        clear_settings_cache()
        async with self.Session() as session:
            self.assertEqual(await get_setting(session, "shop_maintenance_enabled", reseller_id=SHOP), "0")

    async def test_night_mode_comes_back_at_eight_in_the_morning(self) -> None:
        async with self.Session() as session:
            await set_setting(session, CARD.key, "1")
            await run_scenario(
                session, scenario=scenario_by_code("night"), shop_id=None,
                allowed=(CARD,), actor=_actor(None), now=NOW,
            )
            row = await session.scalar(select(SwitchRevert))
        self.assertEqual(row.revert_at.replace(tzinfo=timezone.utc), next_morning(NOW))


class ClockTests(unittest.TestCase):
    def test_next_morning_is_eight_oclock_tehran(self) -> None:
        early = datetime(2026, 11, 18, 2, 0, tzinfo=timezone.utc)  # 05:30 in Tehran
        self.assertEqual(next_morning(early), datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc))
        late = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)  # 09:30 in Tehran
        self.assertEqual(next_morning(late), datetime(2026, 11, 19, 4, 30, tzinfo=timezone.utc))


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_downgrade_and_upgrade_keep_other_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("switch_reverts", ROOT / "alembic/versions/0122_switch_reverts.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)"))
            conn.execute(sa.text("INSERT INTO settings VALUES ('pay_card_enabled', '1')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                indexes = {i["name"] for i in sa.inspect(conn).get_indexes("switch_reverts")}
                self.assertEqual(indexes, {"ix_switch_reverts_status_revert_at", "uq_switch_reverts_pending"})
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("switch_reverts"))
                migration.upgrade()
            self.assertEqual(conn.execute(sa.text("SELECT value FROM settings")).scalar(), "1")
        engine.dispose()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, data: str) -> MagicMock:
        callback = MagicMock()
        callback.data = data
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        return callback

    async def _call(self, handler, data: str, viewer) -> MagicMock:
        callback = self._callback(data)
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
        with patch.object(command_schedules, "load_switch_viewer", AsyncMock(return_value=viewer)), patch.object(
            command_schedules, "run_scenario", AsyncMock(return_value=[])
        ) as runner:
            await handler(callback, MagicMock(), SimpleNamespace(id=5), state, is_reseller_bot=True)
        callback.runner = runner
        return callback

    def _viewer(self, *, actor: bool = True) -> command_switches.SwitchViewer:
        return command_switches.SwitchViewer(SHOP, "nv:res:home", SHOP_SWITCHES if actor else (), _actor(SHOP) if actor else None, None)

    async def test_staff_without_settings_access_cannot_open_scenarios_or_timers(self) -> None:
        for handler, data in ((command_schedules.scenario_screen, "cmd:sc"), (command_schedules.timed_switch, "cmd:tm")):
            callback = await self._call(handler, data, self._viewer(actor=False))
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)

    async def test_a_scenario_with_nothing_the_shop_may_do_is_refused(self) -> None:
        only_card = command_switches.SwitchViewer(SHOP, "nv:res:home", (CARD,), _actor(SHOP), None)
        callback = await self._call(command_schedules.scenario_screen, "cmd:sc:abuse:y", only_card)
        callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)
        callback.runner.assert_not_awaited()

    async def test_unknown_durations_and_malformed_data_are_refused(self) -> None:
        for data in ("cmd:tm:pc:9h", "cmd:tm:pc:1h:x"):
            callback = await self._call(command_schedules.timed_switch, data, self._viewer())
            callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)


if __name__ == "__main__":
    unittest.main()
