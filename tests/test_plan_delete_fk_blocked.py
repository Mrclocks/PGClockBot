"""Plan delete must not 500 on FK dependents — clear Persian block instead.

Root cause: DELETE plans with RESTRICT FKs (user_services / orders / …) raised
IntegrityError; bulk then hit PendingRollbackError on later items → HTTP 500.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _enable_sqlite_fk(dbapi_conn, _connection_record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


class PlanDeleteFkBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "plan_del_fk.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        event.listen(self.engine.sync_engine, "connect", _enable_sqlite_fk)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _seed_plan_with_service(self, session):
        from app.db.models import BotUser, Plan, Role, UserService

        user = BotUser(
            telegram_id=810001,
            role=Role.USER.value,
            referral_code="PD810001",
        )
        plan = Plan(name="پلن وابسته", price=10000, duration_days=30, data_limit_gb=10)
        session.add_all([user, plan])
        await session.commit()
        await session.refresh(user)
        await session.refresh(plan)
        session.add(
            UserService(
                bot_user_id=user.id,
                plan_id=plan.id,
                pg_username="svc-plan-fk",
            )
        )
        await session.commit()
        await session.refresh(plan)
        return plan

    async def test_delete_shop_plan_blocked_with_clear_message(self):
        from app.db.models import Plan
        from app.services.plans_catalog import PlanDeleteBlocked, delete_shop_plan

        async with self.Session() as session:
            plan = await self._seed_plan_with_service(session)
            pid = plan.id
            with self.assertRaises(PlanDeleteBlocked) as ctx:
                await delete_shop_plan(session, plan)
            msg = ctx.exception.message
            self.assertIn("قابل حذف نیست", msg)
            self.assertIn("سرویس کاربر", msg)
            self.assertIn("کاربر", msg)
            # Plan row must still exist
            self.assertIsNotNone(await session.get(Plan, pid))

    async def test_delete_shop_plan_ok_without_deps(self):
        from app.db.models import Plan
        from app.services.plans_catalog import delete_shop_plan

        async with self.Session() as session:
            plan = Plan(name="آزاد", price=5000, duration_days=7)
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            pid = plan.id
            await delete_shop_plan(session, plan)
            await session.commit()
            self.assertIsNone(await session.get(Plan, pid))

    async def test_raw_delete_still_fk_fails(self):
        """Document the failure mode the helper prevents."""
        from sqlalchemy.exc import IntegrityError

        async with self.Session() as session:
            plan = await self._seed_plan_with_service(session)
            await session.delete(plan)
            with self.assertRaises(IntegrityError):
                await session.flush()

    async def test_bulk_delete_survives_blocked_item(self):
        """Blocked plan must not poison the session — free plan still deletes."""
        from app.db.models import Plan
        from app.services.table_bulk_ext import bulk_shop_plan_action

        async with self.Session() as session:
            blocked = await self._seed_plan_with_service(session)
            free = Plan(name="آزاد-bulk", price=1000, duration_days=3)
            session.add(free)
            await session.commit()
            await session.refresh(free)

            # Explicit Owner Principal — required by plan_belongs_to_staff / is_platform_admin
            staff = {
                "role": "admin",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
                "pg_is_owner": True,
            }
            ok, fail, detail = await bulk_shop_plan_action(
                session, staff, [blocked.id, free.id], "delete"
            )
            self.assertEqual(ok, 1)
            self.assertEqual(fail, 1)
            self.assertIsNotNone(detail)
            self.assertIn("قابل حذف نیست", detail or "")
            self.assertIsNotNone(await session.get(Plan, blocked.id))
            self.assertIsNone(await session.get(Plan, free.id))

    async def test_format_mentions_orders_and_services(self):
        from app.services.plans_catalog import format_shop_plan_delete_blocked

        msg = format_shop_plan_delete_blocked(
            {"services": 2, "orders": 5, "points_rules": 0, "funnel_events": 0},
            plan_name="طلایی",
        )
        self.assertIn("طلایی", msg)
        self.assertIn("2 سرویس کاربر", msg)
        self.assertIn("5 سفارش", msg)
        self.assertIn("کاربر دارند", msg)

    async def test_web_and_bulk_paths_use_helper(self):
        root = Path(__file__).resolve().parents[1]
        app_src = (root / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("delete_shop_plan", app_src)
        self.assertIn("PlanDeleteBlocked", app_src)
        bulk = (root / "app/services/table_bulk_ext.py").read_text(encoding="utf-8")
        self.assertIn("delete_shop_plan", bulk)
        self.assertIn("begin_nested", bulk)
        self.assertIn("PlanDeleteBlocked", bulk)


if __name__ == "__main__":
    unittest.main()
