"""The cleanup page says why services past their deadline are still there, and the worker records its failures."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, ServiceAutomation, UserService
from app.services import service_cleanup_delete as delete
from app.services import service_cleanup_notices as notices
from app.services import service_cleanup_report as report
from app.services.pasarguard import PasarGuardError
from app.services.service_cleanup import KIND_TIME, CleanupPolicy, build_preview
from app.services.service_cleanup_errors import ERROR_DELETE_FAILED, ERROR_PANEL_UNREADABLE
from app.services.users import clear_settings_cache
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SHOP = 5
OTHER_SHOP = 6
OLD = NOW - timedelta(days=10)  # expired 10 days ago: due with a 7-day rule
WARNED = NOW - timedelta(days=3)


def policy(**kw) -> CleanupPolicy:
    base = dict(time_on=True, time_days=7, time_enabled_at=NOW - timedelta(days=90), volume_on=False)
    return CleanupPolicy(**{**base, **kw})


def expired() -> dict:
    return {"status": "expired", "expire": int(OLD.timestamp()), "data_limit": 100, "used_traffic": 5}


class Panel:
    def __init__(self, users: dict[int, object], *, delete_error: Exception | None = None) -> None:
        self.users = users
        self.delete_error = delete_error

    async def get_current_admin(self) -> dict:
        return {"username": "shop-admin"}

    async def get_user_by_id(self, user_id: int):
        answer = self.users.get(user_id, PasarGuardError("not found", 404))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def delete_user_by_id(self, user_id: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.users.pop(user_id, None)


class ReportCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=106, referral_code="s6", role="reseller"),
                BotUser(id=20, telegram_id=120, referral_code="c20", reseller_id=SHOP),
                BotUser(id=21, telegram_id=121, referral_code="c21", reseller_id=OTHER_SHOP),
                BotUser(id=22, telegram_id=122, referral_code="c22", reseller_id=SHOP, is_blocked=True),
                BotUser(id=23, telegram_id=0, referral_code="c23", reseller_id=SHOP),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        clear_settings_cache()

    async def add(self, service_id: int, owner: int = 20, shop: int | None = SHOP, **fields) -> None:
        fields.setdefault("cleanup_warn_stage", 1)
        fields.setdefault("cleanup_warned_at", WARNED)
        fields.setdefault("quota_expire_at", OLD)
        async with self.Session() as session:
            session.add(UserService(id=service_id, bot_user_id=owner, pg_user_id=1000 + service_id,
                                    pg_username=f"svc{service_id}", shop_id=shop, **fields))
            await session.commit()

    async def summary(self, shop: int | None = SHOP) -> dict[str, tuple[int, str]]:
        async with self.Session() as session:
            items = await report.reason_summary(session, shop_id=shop, policy=policy(), now=NOW)
        return {item.code: (item.count, item.last_at) for item in items}

    async def error_of(self, service_id: int) -> str | None:
        async with self.Session() as session:
            return (await session.get(UserService, service_id)).cleanup_error


class ReasonSummaryTests(ReportCase):
    async def test_each_due_service_is_counted_once_under_its_first_reason(self) -> None:
        await self.add(1)
        await self.add(2)
        await self.add(3, owner=22)
        await self.add(4, owner=23, cleanup_warn_stage=0, cleanup_warned_at=None)
        await self.add(5, cleanup_warn_stage=0, cleanup_warned_at=None)
        await self.add(6)
        await self.add(7, cleanup_error=ERROR_PANEL_UNREADABLE, cleanup_error_at=NOW - timedelta(hours=1))
        await self.add(8, quota_expire_at=NOW - timedelta(days=2))  # expired but not due yet
        await self.add(9, cleanup_exempt=True)
        async with self.Session() as session:
            session.add(Order(user_id=20, service_id=1, amount=100, status="pending", reseller_id=SHOP))
            session.add(Order(user_id=20, service_id=6, amount=100, status="delivered", reseller_id=SHOP))
            session.add(ServiceAutomation(service_id=2, shop_id=SHOP, renew_enabled=True, renew_plan_id=None))
            await session.commit()
        self.assertEqual(await self.summary(), {
            ERROR_PANEL_UNREADABLE: (1, "2026-11-10"),
            report.OPEN_ORDER: (1, ""),
            report.AUTO_RENEW: (1, ""),
            report.CUSTOMER_BLOCKED: (1, ""),
            report.NO_CHAT: (1, ""),
            report.AWAITING_NOTICE: (1, ""),
            report.NEXT_RUN: (1, ""),
        })

    async def test_failures_are_listed_first_with_their_latest_date(self) -> None:
        await self.add(1, cleanup_error=ERROR_DELETE_FAILED, cleanup_error_at=NOW - timedelta(days=4))
        await self.add(2, cleanup_error=ERROR_DELETE_FAILED, cleanup_error_at=NOW - timedelta(days=1))
        await self.add(3)
        async with self.Session() as session:
            items = await report.reason_summary(session, shop_id=SHOP, policy=policy(), now=NOW)
        self.assertEqual([(i.code, i.count, i.last_at, i.tone) for i in items], [
            (ERROR_DELETE_FAILED, 2, "2026-11-09", "danger"),
            (report.NEXT_RUN, 1, "", "info"),
        ])

    async def test_another_shop_and_the_platform_are_never_counted(self) -> None:
        await self.add(1, owner=21, shop=OTHER_SHOP, cleanup_error=ERROR_PANEL_UNREADABLE, cleanup_error_at=NOW)
        await self.add(2, owner=21, shop=OTHER_SHOP)
        await self.add(3, shop=None)
        self.assertEqual(await self.summary(), {})
        self.assertEqual(await self.summary(None), {report.NEXT_RUN: (1, "")})

    async def test_a_rule_that_is_off_has_no_held_services(self) -> None:
        await self.add(1)
        async with self.Session() as session:
            items = await report.reason_summary(session, shop_id=SHOP, policy=policy(time_on=False), now=NOW)
        self.assertEqual(items, [])


class RowReasonTests(ReportCase):
    async def test_only_rows_past_their_deadline_get_a_reason(self) -> None:
        await self.add(1, cleanup_error=ERROR_PANEL_UNREADABLE, cleanup_error_at=NOW)
        await self.add(2, cleanup_warn_stage=0, cleanup_warned_at=None)
        await self.add(3, quota_expire_at=NOW - timedelta(days=2))
        async with self.Session() as session:
            preview = await build_preview(session, shop_id=SHOP, policy=policy(), now=NOW)
            reasons = await report.due_row_reasons(session, preview.rows, now=NOW)
        self.assertEqual({sid: r.code for sid, r in reasons.items()}, {1: ERROR_PANEL_UNREADABLE, 2: report.AWAITING_NOTICE})


class WorkerRecordsFailuresTests(ReportCase):
    async def run_step(self, step, panel: Panel, **kw):
        with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)):
            async with self.Session() as session:
                return await step(session, shop_id=SHOP, policy=policy(), kind=KIND_TIME, now=NOW, **kw)

    async def test_the_warning_scan_records_an_unreadable_panel_and_clears_it_after_a_good_read(self) -> None:
        await self.add(1, cleanup_warn_stage=0, cleanup_warned_at=None, quota_expire_at=NOW - timedelta(days=2))
        await self.run_step(notices.scan_notices, Panel({1001: TimeoutError()}))
        self.assertEqual(await self.error_of(1), ERROR_PANEL_UNREADABLE)
        await self.run_step(notices.scan_notices, Panel({1001: expired()}))
        self.assertIsNone(await self.error_of(1))

    async def test_the_delete_step_records_an_unreadable_panel(self) -> None:
        await self.add(1)
        await self.run_step(delete.delete_due_services, Panel({1001: PasarGuardError("bad gateway", 502)}))
        self.assertEqual(await self.error_of(1), ERROR_PANEL_UNREADABLE)

    async def test_a_refused_delete_is_recorded_and_the_service_kept(self) -> None:
        await self.add(1)
        result = await self.run_step(
            delete.delete_due_services, Panel({1001: expired()}, delete_error=PasarGuardError("forbidden", 403))
        )
        self.assertEqual(result.failed, 1)
        self.assertEqual(await self.error_of(1), ERROR_DELETE_FAILED)

    async def test_a_deleted_service_leaves_nothing_behind(self) -> None:
        await self.add(1, cleanup_error=ERROR_PANEL_UNREADABLE, cleanup_error_at=NOW - timedelta(days=1))
        result = await self.run_step(delete.delete_due_services, Panel({1001: expired()}))
        self.assertEqual(result.deleted, 1)
        self.assertEqual(await self.summary(), {})


class CleanupErrorMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("m0145", ROOT / "alembic/versions/0145_service_cleanup_errors.py")
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        self.addCleanup(engine.dispose)
        added = {"cleanup_error", "cleanup_error_at"}
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, shop_id INTEGER, pg_username TEXT)"))
            conn.execute(sa.text("INSERT INTO user_services (id, shop_id, pg_username) VALUES (1, 5, 'old')"))
            for down in (False, False, True):
                with Operations.context(MigrationContext.configure(conn)):
                    (migration.downgrade if down else migration.upgrade)()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("user_services")}
                indexes = {i["name"] for i in sa.inspect(conn).get_indexes("user_services")}
                if not down:
                    self.assertTrue(added <= columns)
                    self.assertIn("ix_user_services_cleanup_error", indexes)
            self.assertFalse(added & columns)
            self.assertNotIn("ix_user_services_cleanup_error", indexes)
            self.assertEqual(conn.execute(sa.text("SELECT pg_username FROM user_services")).scalar(), "old")


if __name__ == "__main__":
    unittest.main()
