"""Shop staff change a customer's loyalty points by hand; the owner form is applied once per submit."""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.shop_points_adjust as points_module
from app.services.shop_manual_adjust import ManualAdjustError
from app.services.shop_points_adjust import (
    adjust_shop_points,
    parse_points_request,
    points_adjust_shop,
    shop_points_form_context,
)

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 81
SHOP_B = 82
STAFF_TG = 990_001
OWNER = {
    "role": "admin", "id": 1, "username": "root", "org_principal_id": 1,
    "org_depth": 0, "org_parent_id": None, "org_status": "active",
}


def _reseller(shop_id: int, perms: list[str] | None = None) -> dict:
    return {
        "role": "reseller", "bot_user_id": shop_id, "id": shop_id, "username": f"shop{shop_id}",
        "permissions": ["loyalty", "loyalty_adjust"] if perms is None else perms,
    }


def _request(customer_id: int, points: int = 100, direction: str = "credit", key: str | None = None):
    return parse_points_request(
        raw_customer_id=str(customer_id), raw_points=str(points), direction=direction,
        reason="birthday bonus", request_key=key or secrets.token_hex(16),
    )


class PointsPermissionTests(unittest.TestCase):
    def test_only_shop_staff_with_the_key(self):
        self.assertEqual(points_adjust_shop(_reseller(SHOP_A)), SHOP_A)
        for staff in (_reseller(SHOP_A, ["loyalty"]), OWNER, {"role": "pg_staff", "permissions": ["loyalty_adjust"]}):
            with self.subTest(staff=staff), self.assertRaises(PermissionError):
                points_adjust_shop(staff)
        self.assertEqual(shop_points_form_context(_reseller(SHOP_A, ["loyalty"])), {"can_adjust_shop_points": False})

    def test_bad_values_are_refused(self):
        good = {"raw_customer_id": "5", "raw_points": "10", "direction": "credit", "reason": "why not", "request_key": secrets.token_hex(16)}
        self.assertEqual(parse_points_request(**good).points, 10)
        for field, value in (
            ("raw_customer_id", "0"), ("raw_customer_id", "-1"), ("raw_customer_id", "9" * 13),
            ("raw_points", "0"), ("raw_points", str(points_module.POINTS_MAX + 1)), ("raw_points", "1.5"),
            ("raw_points", True), ("direction", "set"), ("reason", ""), ("request_key", "nope"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ManualAdjustError):
                parse_points_request(**{**good, field: value})


class PointsDbHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser, BotUserShop, ResellerProfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'points.db'}", connect_args={"timeout": 15}
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_A, is_active=True, bot_admin_ids=str(STAFF_TG)))
            session.add(ResellerProfile(user_id=SHOP_B, is_active=True))
            session.add_all([
                BotUser(id=101, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A, points_balance=40),
                BotUser(id=102, telegram_id=1020, referral_code="c102", reseller_id=SHOP_B),
                BotUser(id=103, telegram_id=1030, referral_code="c103", reseller_id=SHOP_B),
                BotUser(id=104, telegram_id=STAFF_TG, referral_code="c104", reseller_id=SHOP_A),
            ])
            await session.flush()
            session.add(BotUserShop(bot_user_id=103, shop_id=SHOP_A))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _points(self, user_id: int, shop_id: int | None) -> int:
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            return await points_balance(session, await session.get(BotUser, user_id), shop_id)

    async def _count(self, model) -> int:
        async with self.Session() as session:
            return int((await session.execute(select(func.count()).select_from(model))).scalar_one())

    async def _adjust(self, staff: dict, request):
        async with self.Session() as session:
            return await adjust_shop_points(session, staff=staff, request=request)


class ShopPointsAdjustTests(PointsDbHarness):
    async def test_credit_moves_only_this_shops_points_and_is_recorded(self):
        from app.db.models import PointsTransaction, ShopStaffAction

        change = await self._adjust(_reseller(SHOP_A), _request(101, 250))
        self.assertEqual((change.balance_after, change.replayed), (250, False))
        self.assertEqual(await self._points(101, SHOP_A), 250)
        self.assertEqual(await self._points(101, None), 40)
        async with self.Session() as session:
            record = (await session.execute(select(ShopStaffAction))).scalar_one()
            ledger = (await session.execute(select(PointsTransaction))).scalar_one()
        self.assertEqual((record.shop_id, record.action, record.amount), (SHOP_A, "points_credit", 250))
        self.assertEqual((ledger.reseller_id, ledger.amount, ledger.source), (SHOP_A, 250, "shop_staff"))

    async def test_repeated_and_concurrent_submits_apply_once(self):
        request = _request(101, 100)
        results = await asyncio.gather(*(self._adjust(_reseller(SHOP_A), request) for _ in range(4)), return_exceptions=True)
        again = await self._adjust(_reseller(SHOP_A), request)
        self.assertTrue(again.replayed)
        self.assertEqual(sum(1 for r in results if not isinstance(r, Exception) and not r.replayed), 1)
        self.assertEqual(await self._points(101, SHOP_A), 100)

    async def test_daily_cap_and_shortfall_change_nothing(self):
        from app.db.models import ShopStaffAction

        with mock.patch.object(points_module, "POINTS_DAILY_CAP", 300):
            await self._adjust(_reseller(SHOP_A), _request(101, 200))
            with self.assertRaises(ManualAdjustError):
                await self._adjust(_reseller(SHOP_A), _request(101, 200))
            await self._adjust(_reseller(SHOP_A), _request(101, 90, "debit"))  # 290 of 300 used
            with self.assertRaises(ManualAdjustError):
                await self._adjust(_reseller(SHOP_A), _request(101, 20, "debit"))  # would pass 300
        with self.assertRaises(ManualAdjustError):
            await self._adjust(_reseller(SHOP_A), _request(101, 201, "debit"))
        self.assertEqual(await self._points(101, SHOP_A), 200 - 90)
        self.assertEqual(await self._count(ShopStaffAction), 2)

    async def test_other_shops_and_staff_are_refused(self):
        from app.db.models import PointsTransaction

        for customer_id in (102, SHOP_A, 104, 99_999):
            with self.subTest(customer_id=customer_id), self.assertRaises(ManualAdjustError):
                await self._adjust(_reseller(SHOP_A), _request(customer_id))
        self.assertEqual(await self._count(PointsTransaction), 0)
        await self._adjust(_reseller(SHOP_A), _request(103, 50))
        self.assertEqual((await self._points(103, SHOP_A), await self._points(103, SHOP_B)), (50, 0))


class ShopPointsRouteTests(PointsDbHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.shop_points_pages import register_shop_points_pages

        self.staff = _reseller(SHOP_A)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_shop_points_pages(app, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def _post(self, **data):
        form = {"user_id": "101", "points": "30", "mode": "credit", "reason": "goodwill", "nonce": secrets.token_hex(16), **data}
        return await self.client.post("/loyalty/shop-adjust", data=form)

    async def test_route(self):
        self.assertIn("ok=", (await self._post()).headers["location"])
        self.assertIn("err=", (await self._post(user_id="102")).headers["location"])
        self.staff = _reseller(SHOP_A, ["loyalty"])
        self.assertEqual((await self._post()).headers["location"], "/home")
        self.assertEqual(await self._points(101, SHOP_A), 30)


class OwnerPointsFormTests(PointsDbHarness):
    """The owner's /loyalty/adjust uses the form's key: a repeated submit is applied once."""

    async def test_owner_repeat_submit_applies_once(self):
        from app.db.models import BotUser
        from app.services.loyalty import admin_adjust_points

        key = secrets.token_hex(16)
        async with self.Session() as session:
            user = await session.get(BotUser, 101)
            await admin_adjust_points(session, user, 5, reason="fix", admin_identity="root", shop_id=None, request_key=key)
        async with self.Session() as session:
            user = await session.get(BotUser, 101)
            await admin_adjust_points(session, user, 5, reason="fix", admin_identity="root", shop_id=None, request_key=key)
        self.assertEqual(await self._points(101, None), 45)


class PointsMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("points_days", ROOT / "alembic/versions/0080_shop_points_adjust_days.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            self.assertIn("shop_points_adjust_days", sa.inspect(conn).get_table_names())
            migration.downgrade()
            migration.downgrade()
            self.assertNotIn("shop_points_adjust_days", sa.inspect(conn).get_table_names())
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
