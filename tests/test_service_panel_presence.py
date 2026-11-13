"""A service is «removed from panel» only after a clean 404; outages never mark it."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, UserService
from app.services import service_panel_presence as presence
from app.services.panel_alert_notifications import claim_signal, clear_signal
from app.services.pasarguard import PasarGuardError

ROOT = Path(__file__).resolve().parents[1]


class FakePanel:
    """Panel client whose answer per user id is a dict, or an exception to raise."""

    def __init__(self, answers: dict[int, object]) -> None:
        self.answers = answers
        self.calls: list[int] = []
        self.admin: object = {"username": "shop-admin"}

    async def get_current_admin(self) -> dict | None:
        if isinstance(self.admin, BaseException):
            raise self.admin
        return self.admin  # type: ignore[return-value]

    async def get_user_by_id(self, user_id: int) -> dict:
        self.calls.append(user_id)
        answer = self.answers[user_id]
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]


class PresenceFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            owner = BotUser(telegram_id=1, referral_code="r1")
            other = BotUser(telegram_id=2, referral_code="r2")
            session.add_all([owner, other])
            await session.flush()
            self.owner_id, self.other_id = owner.id, other.id
            session.add_all(
                [
                    UserService(bot_user_id=owner.id, pg_user_id=10, pg_username="gone", shop_id=None),
                    UserService(bot_user_id=owner.id, pg_user_id=11, pg_username="alive", shop_id=None),
                    UserService(bot_user_id=owner.id, pg_user_id=12, pg_username="outage", shop_id=None),
                    UserService(bot_user_id=other.id, pg_user_id=13, pg_username="shopsvc", shop_id=other.id),
                ]
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def flags(self) -> dict[str, bool]:
        async with self.Session() as session:
            rows = (await session.execute(sa.select(UserService.pg_username, UserService.panel_missing_at))).all()
        return {name: at is not None for name, at in rows}

    def panel(self) -> FakePanel:
        return FakePanel(
            {
                10: PasarGuardError("nf", 404),
                11: {"id": 11, "status": "active"},
                12: PasarGuardError("bad gateway", 502),
                13: PasarGuardError("nf", 404),
            }
        )

    async def scan(self, panel: FakePanel) -> None:
        with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)):
            async with self.Session() as session:
                await presence.scan_panel_presence(session)


class PresenceCase(PresenceFixture):
    async def test_only_a_clean_404_marks_the_service_missing(self) -> None:
        await self.scan(self.panel())
        flags = await self.flags()
        self.assertTrue(flags["gone"])
        self.assertFalse(flags["alive"])
        self.assertFalse(flags["outage"])

    async def test_a_404_is_ignored_when_the_client_cannot_read_its_own_admin(self) -> None:
        for broken in (PasarGuardError("expired", 401), None):
            panel = self.panel()
            panel.admin = broken
            await self.scan(panel)
            self.assertFalse((await self.flags())["gone"], broken)

    async def test_a_service_that_comes_back_is_unmarked(self) -> None:
        await self.scan(self.panel())
        panel = self.panel()
        panel.answers[10] = {"id": 10, "status": "active"}
        await self.scan(panel)
        self.assertFalse((await self.flags())["gone"])

    async def test_an_outage_keeps_an_existing_mark_and_a_repeat_scan_is_stable(self) -> None:
        await self.scan(self.panel())
        panel = self.panel()
        panel.answers[10] = PasarGuardError("timeout", 504)
        await self.scan(panel)
        await self.scan(panel)
        self.assertTrue((await self.flags())["gone"])

    async def test_the_scan_takes_the_least_recently_checked_first_and_is_bounded(self) -> None:
        panel = self.panel()
        with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)):
            async with self.Session() as session:
                results = await presence.scan_panel_presence(session, limit=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(panel.calls)), 2)

    async def test_services_that_never_answer_cannot_starve_the_rest_of_the_scan(self) -> None:
        panel = self.panel()
        panel.answers = {10: PasarGuardError("down", 502), 11: PasarGuardError("down", 502), 12: PasarGuardError("down", 502), 13: {"id": 13}}
        for _ in range(2):
            with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)):
                async with self.Session() as session:
                    await presence.scan_panel_presence(session, limit=2)
        self.assertEqual(set(panel.calls), {10, 11, 12, 13})

    async def test_alert_counts_are_per_shop(self) -> None:
        await self.scan(self.panel())
        async with self.Session() as session:
            signals = await presence.missing_signals(session)
        by_shop = {s.shop_id: s.count for s in signals}
        self.assertEqual(by_shop, {None: 1, self.other_id: 1})

    async def test_a_cancelled_service_is_not_scanned(self) -> None:
        async with self.Session() as session:
            await session.execute(sa.update(UserService).where(UserService.pg_username == "gone").values(is_cancelled=True))
            await session.commit()
        panel = self.panel()
        await self.scan(panel)
        self.assertNotIn(10, panel.calls)
        self.assertFalse((await self.flags())["gone"])


class MissingAlertCadenceTests(PresenceFixture):
    """Staff hear about services removed while usable: once, then only for new ones, at most daily."""

    BASE = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    async def mark(self, name: str, at: datetime, **quota: object) -> None:
        async with self.Session() as session:
            await session.execute(
                sa.update(UserService).where(UserService.pg_username == name).values(panel_missing_at=at, **quota)
            )
            await session.commit()

    async def drop(self, name: str) -> None:
        async with self.Session() as session:
            await session.execute(sa.delete(UserService).where(UserService.pg_username == name))
            await session.commit()

    async def tick(self, now: datetime) -> list[int]:
        """Run one alert round for the platform scope; returns the counts that would be sent."""
        sent: list[int] = []
        async with self.Session() as session:
            for signal in await presence.missing_signals(session):
                if signal.shop_id is not None:
                    continue
                if signal.count <= 0:
                    await clear_signal(session, signal)
                elif await claim_signal(session, signal, now=now):
                    sent.append(signal.count)
        return sent

    async def test_services_the_panel_removed_after_they_ended_are_not_announced(self) -> None:
        await self.mark("gone", self.BASE, quota_expire_at=self.BASE - timedelta(days=3))
        await self.mark("alive", self.BASE, volume_exhausted_at=self.BASE - timedelta(days=1))
        self.assertEqual(await self.tick(self.BASE), [])

    async def test_a_service_removed_before_it_expired_is_announced(self) -> None:
        await self.mark("gone", self.BASE, quota_expire_at=self.BASE + timedelta(days=10))
        await self.mark("alive", self.BASE)
        self.assertEqual(await self.tick(self.BASE), [2])

    async def test_cleaning_up_announced_services_sends_nothing_new(self) -> None:
        await self.mark("gone", self.BASE)
        await self.mark("alive", self.BASE + timedelta(minutes=1))
        self.assertEqual(await self.tick(self.BASE + timedelta(minutes=5)), [2])
        await self.drop("alive")
        for hours in (1, 7, 13, 30):
            self.assertEqual(await self.tick(self.BASE + timedelta(hours=hours)), [], hours)
        await self.drop("gone")
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=31)), [])
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=60)), [])

    async def test_new_removals_are_announced_at_most_once_a_day(self) -> None:
        await self.mark("gone", self.BASE)
        self.assertEqual(await self.tick(self.BASE + timedelta(minutes=5)), [1])
        await self.mark("alive", self.BASE + timedelta(hours=2))
        await self.mark("outage", self.BASE + timedelta(hours=8))
        for hours in (6, 12, 18, 23):
            self.assertEqual(await self.tick(self.BASE + timedelta(hours=hours)), [], hours)
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=24, minutes=6)), [3])
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=60)), [])

    async def test_a_new_removal_after_a_full_cleanup_waits_for_the_daily_interval(self) -> None:
        await self.mark("gone", self.BASE)
        self.assertEqual(await self.tick(self.BASE + timedelta(minutes=5)), [1])
        await self.drop("gone")
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=1)), [])
        await self.mark("alive", self.BASE + timedelta(hours=2))
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=3)), [])
        self.assertEqual(await self.tick(self.BASE + timedelta(hours=25)), [1])

    async def test_a_shop_alert_ignores_another_shops_removals(self) -> None:
        await self.mark("shopsvc", self.BASE)
        self.assertEqual(await self.tick(self.BASE), [])
        async with self.Session() as session:
            by_shop = {s.shop_id: s.count for s in await presence.missing_signals(session)}
        self.assertEqual(by_shop, {self.other_id: 1})


class PresenceMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("m0104", ROOT / "alembic/versions/0104_service_panel_presence.py")
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, shop_id INTEGER, pg_username TEXT)"))
            conn.execute(sa.text("INSERT INTO user_services (id, shop_id, pg_username) VALUES (1, NULL, 'old')"))
            for down in (False, False, True):
                with Operations.context(MigrationContext.configure(conn)):
                    (migration.downgrade if down else migration.upgrade)()
                if not down:
                    cols = {c["name"] for c in sa.inspect(conn).get_columns("user_services")}
                    self.assertTrue({"panel_missing_at", "panel_checked_at"} <= cols)
            cols = {c["name"] for c in sa.inspect(conn).get_columns("user_services")}
            self.assertFalse({"panel_missing_at", "panel_checked_at"} & cols)
            self.assertEqual(conn.execute(sa.text("SELECT pg_username FROM user_services")).scalar(), "old")


if __name__ == "__main__":
    unittest.main()
