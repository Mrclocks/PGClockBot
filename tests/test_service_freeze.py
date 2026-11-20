"""Customer service freeze: time left is kept, every step is claimed first and retried with the same numbers."""

from __future__ import annotations

import asyncio
import importlib.util
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, CustomerNotice, ServiceFreeze, UserService
from app.jobs.service_freezes import _queue_resumed_notices
from app.services import service_freeze as freeze
from app.services.service_freeze_settings import freeze_policy
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
UI = {"service_freeze_enabled": "1", "service_freeze_max_days": "14", "service_freeze_per_period": "1"}


class FakePG:
    def __init__(self) -> None:
        self.info = {"status": "active", "expire": int(time.time()) + 10 * DAY, "data_limit": 0, "used_traffic": 0}
        self.fail_disable = False
        self.fail_modify = False
        self.modifies: list[dict] = []

    async def get_user_by_id(self, user_id: int) -> dict:
        return dict(self.info)

    async def set_disabled_by_id(self, user_id: int, disabled: bool) -> dict:
        if self.fail_disable:
            raise TimeoutError("lost")
        self.info["status"] = "disabled" if disabled else "active"
        return dict(self.info)

    async def modify_user_by_id(self, user_id: int, payload: dict) -> dict:
        self.modifies.append(dict(payload))
        self.info.update(payload)
        if self.fail_modify:
            raise TimeoutError("response lost after the write")
        return dict(self.info)


class FreezeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.pg = FakePG()
        self.patch = patch("app.services.service_freeze.get_pg", return_value=self.pg)
        self.patch.start()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=1001, referral_code="c1"),
                BotUser(id=2, telegram_id=1002, referral_code="c2"),
                BotUser(id=7, telegram_id=1007, referral_code="shop", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                UserService(id=10, bot_user_id=1, pg_user_id=500, pg_username="svc"),
                UserService(id=11, bot_user_id=1, pg_user_id=501, pg_username="shop-svc", shop_id=7),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        self.patch.stop()
        await self.engine.dispose()

    async def start(self, *, user_id: int = 1, service_id: int = 10, days: int = 7, ui: dict | None = None,
                    shop_id: int | None = None) -> ServiceFreeze:
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            return await freeze.start_freeze(session, user, service_id, shop_id=shop_id, days=days, ui=ui or UI)

    async def resume(self) -> ServiceFreeze:
        async with self.Session() as session:
            return await freeze.resume_freeze(session, await session.get(BotUser, 1), 10, shop_id=None)

    async def rows(self) -> list[ServiceFreeze]:
        async with self.Session() as session:
            return list(await session.scalars(select(ServiceFreeze).order_by(ServiceFreeze.id)))

    async def job(self, *, now: datetime | None = None) -> list[ServiceFreeze]:
        async with self.Session() as session:
            return await freeze.process_due_freezes(session, now=now)

    async def test_an_early_resume_keeps_the_time_that_was_left(self) -> None:
        left = self.pg.info["expire"] - int(time.time())
        row = await self.start()
        self.assertEqual((row.status, self.pg.info["status"]), ("frozen", "disabled"))
        self.assertAlmostEqual(row.remaining_seconds, left, delta=5)
        done = await self.resume()
        self.assertEqual((done.status, self.pg.info["status"]), ("done", "active"))
        self.assertAlmostEqual(self.pg.info["expire"], int(time.time()) + left, delta=5)
        async with self.Session() as session:
            service = await session.get(UserService, 10)
            self.assertAlmostEqual(service.quota_expire_at.timestamp(), self.pg.info["expire"], delta=1)
            self.assertGreater(service.cleanup_hold_until, datetime.now(timezone.utc) + timedelta(days=7))

    async def test_the_job_resumes_at_the_deadline_and_queues_a_notice(self) -> None:
        await self.start(days=3)
        self.assertEqual(await self.job(), [])
        resumed = await self.job(now=datetime.now(timezone.utc) + timedelta(days=3, minutes=1))
        self.assertEqual([(r.status, r.resumed_by) for r in await self.rows()], [("done", "deadline")])
        async with self.Session() as session:
            await _queue_resumed_notices(session, resumed)
            notices = list(await session.scalars(select(CustomerNotice)))
        self.assertEqual([(n.action, n.service_id, n.telegram_id) for n in notices], [("service_freeze_resumed", 10, 1001)])

    async def test_a_lost_disable_is_retried_by_the_job(self) -> None:
        self.pg.fail_disable = True
        self.assertEqual((await self.start()).status, "pausing")
        self.pg.fail_disable = False
        await self.job()
        self.assertEqual([r.status for r in await self.rows()], ["frozen"])
        self.assertEqual(self.pg.info["status"], "disabled")

    async def test_a_lost_resume_is_retried_with_the_same_expiry(self) -> None:
        await self.start()
        self.pg.fail_modify = True
        row = await self.resume()
        self.assertEqual(row.status, "resuming")
        self.pg.fail_modify = False
        await asyncio.sleep(1.1)
        await self.job()
        self.assertEqual([r.status for r in await self.rows()], ["done"])
        self.assertEqual({m["expire"] for m in self.pg.modifies}, {row.target_expire})

    async def test_one_open_freeze_and_the_period_limit(self) -> None:
        results = await asyncio.gather(self.start(), self.start(), return_exceptions=True)
        self.assertEqual(sorted(type(r).__name__ for r in results), ["ServiceFreeze", "ValueError"])
        await self.resume()
        with self.assertRaisesRegex(ValueError, "سقف"):
            await self.start()
        self.assertEqual(len(await self.rows()), 1)

    async def test_switched_off_and_out_of_range_days_are_refused(self) -> None:
        for kwargs in ({"ui": {"service_freeze_enabled": "0"}}, {"days": 15}, {"days": 0}, {"days": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.start(**kwargs)
        self.assertEqual(await self.rows(), [])

    async def test_another_customer_or_shop_cannot_freeze_or_resume(self) -> None:
        with self.assertRaises(ValueError):
            await self.start(user_id=2)
        with self.assertRaises(ValueError):
            await self.start(service_id=11)
        await self.start()
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await freeze.resume_freeze(session, await session.get(BotUser, 2), 10, shop_id=None)
            with self.assertRaises(ValueError):
                await freeze.resume_freeze(session, await session.get(BotUser, 1), 10, shop_id=7)

    async def test_services_that_cannot_be_frozen(self) -> None:
        now = int(time.time())
        cases = (
            {"expire": None}, {"status": "expired"}, {"expire": now + 3000},
            {"data_limit": 10, "used_traffic": 10},
        )
        for info in cases:
            self.pg.info = {"status": "active", "expire": now + 10 * DAY, "data_limit": 0, "used_traffic": 0, **info}
            with self.subTest(info=info), self.assertRaises(ValueError):
                await self.start()
        self.assertEqual(await self.rows(), [])

    async def test_the_more_menu_shows_pause_then_resume(self) -> None:
        async with self.Session() as session:
            service = await session.get(UserService, 10)
            self.assertEqual(await freeze.freeze_button(session, service, UI), "pause")
            self.assertIsNone(await freeze.freeze_button(session, service, {}))
        await self.start()
        async with self.Session() as session:
            self.assertEqual(await freeze.freeze_button(session, await session.get(UserService, 10), {}), "resume")

    def test_the_more_keyboard_carries_the_freeze_button(self) -> None:
        from app.bot.nav_inline import service_more_keyboard

        data = lambda freeze_state: [b.callback_data for row in service_more_keyboard(10, {}, freeze=freeze_state).inline_keyboard for b in row]  # noqa: E731
        self.assertIn("svc:frz:10", data("pause"))
        self.assertIn("svc:frzres:10", data("resume"))
        self.assertFalse([d for d in data(None) if d.startswith("svc:frz")])

    def test_policy_clamps_and_offers_day_choices(self) -> None:
        policy = freeze_policy({"service_freeze_enabled": "1", "service_freeze_max_days": "500"})
        self.assertEqual((policy.max_days, policy.day_choices()), (90, [3, 7, 14, 30, 90]))
        self.assertEqual(freeze_policy({"service_freeze_max_days": "5"}).day_choices(), [3, 5])


class FreezeMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        spec = importlib.util.spec_from_file_location("freeze", ROOT / "alembic/versions/0125_service_freezes.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO bot_users VALUES (1)"))
            conn.execute(text("INSERT INTO user_services VALUES (10)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                insert = text("INSERT INTO service_freezes (service_id, user_id, status, remaining_seconds, resume_by) "
                              "VALUES (10, 1, :status, 60, now())")
                conn.execute(insert, {"status": "frozen"})
                conn.execute(insert, {"status": "done"})
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(insert, {"status": "pausing"})
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("service_freezes"))
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
