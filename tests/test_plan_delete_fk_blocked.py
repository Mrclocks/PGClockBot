"""Plan delete must not 500 on FK dependents.

Shop sales plans detach historical refs (orders/services/…) so the catalog
row can be removed; only mid-delivery paid orders still block. Reseller
package plans keep a hard FK pre-check with a clear Persian message.
Bulk uses nested savepoints so one blocked item cannot poison the session.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class PlanDeleteFkBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

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
        return plan, user

    async def test_delete_shop_plan_detaches_service_refs(self):
        from app.db.models import Plan, UserService
        from app.services.plans_catalog import delete_shop_plan
        from sqlalchemy import select

        async with self.Session() as session:
            plan, _user = await self._seed_plan_with_service(session)
            pid = plan.id
            await delete_shop_plan(session, plan)
            await session.commit()
            self.assertIsNone(await session.get(Plan, pid))
            svc = (
                await session.execute(
                    select(UserService).where(UserService.pg_username == "svc-plan-fk")
                )
            ).scalar_one()
            self.assertIsNone(svc.plan_id)

    async def test_delete_shop_plan_blocked_while_paid_pending(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.plans_catalog import PlanDeleteBlocked, delete_shop_plan

        async with self.Session() as session:
            plan, user = await self._seed_plan_with_service(session)
            session.add(
                Order(
                    user_id=user.id,
                    plan_id=plan.id,
                    amount=int(plan.price or 0),
                    status=OrderStatus.PAID.value,
                )
            )
            await session.commit()
            await session.refresh(plan)
            pid = plan.id
            with self.assertRaises(PlanDeleteBlocked) as ctx:
                await delete_shop_plan(session, plan)
            self.assertIn("انتظار تحویل", ctx.exception.message)
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
            plan, _user = await self._seed_plan_with_service(session)
            await session.delete(plan)
            with self.assertRaises(IntegrityError):
                await session.flush()

    async def test_bulk_delete_detaches_and_continues(self):
        from app.db.models import Plan
        from app.services.table_bulk_ext import bulk_shop_plan_action

        async with self.Session() as session:
            linked, _user = await self._seed_plan_with_service(session)
            free = Plan(name="آزاد-bulk", price=1000, duration_days=3)
            session.add(free)
            await session.commit()
            await session.refresh(free)

            staff = {
                "role": "admin",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
                "pg_is_owner": True,
            }
            ok, fail, detail = await bulk_shop_plan_action(
                session, staff, [linked.id, free.id], "delete"
            )
            self.assertEqual(ok, 2)
            self.assertEqual(fail, 0)
            self.assertIsNone(detail)
            self.assertIsNone(await session.get(Plan, linked.id))
            self.assertIsNone(await session.get(Plan, free.id))

    async def test_reseller_format_mentions_profiles(self):
        from app.services.plans_catalog import format_reseller_plan_delete_blocked

        msg = format_reseller_plan_delete_blocked(
            {"profiles": 2, "subscriptions": 1, "applications": 0},
            plan_name="نقره‌ای",
        )
        self.assertIn("نقره‌ای", msg)
        self.assertIn("2 نماینده", msg)
        self.assertIn("1 اشتراک پاسارگارد", msg)

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
