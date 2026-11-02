"""A shop blocks a customer in that shop only: its bot, its Mini App and its campaigns."""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

import httpx
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.shop_customer_block import (
    ACCOUNT_BLOCKED_MESSAGE,
    BLOCKED_MESSAGE,
    block_shop,
    blocked_notice,
    is_blocked_in_shop,
    may_block,
    set_shop_block,
    shop_block_form_context,
)
from app.services.shop_manual_adjust import ManualAdjustError

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
        "permissions": ["customers_block"] if perms is None else perms,
    }


class BlockPermissionTests(unittest.TestCase):
    def test_only_shop_staff_with_the_key(self):
        self.assertEqual(block_shop(_reseller(SHOP_A)), SHOP_A)
        for staff in (_reseller(SHOP_A, ["dashboard"]), OWNER, {"role": "pg_staff", "permissions": ["customers_block"]}):
            with self.subTest(staff=staff):
                self.assertFalse(may_block(staff))


class BlockDbHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser, BotUserShop, ResellerProfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'block.db'}", connect_args={"timeout": 15}
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
                BotUser(id=101, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A),
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

    async def _set(self, staff: dict, customer_id: int, blocked: bool, reason: str = "abusive messages") -> bool:
        async with self.Session() as session:
            return await set_shop_block(session, staff=staff, customer_id=customer_id, blocked=blocked, reason=reason)

    async def _blocked(self, customer_id: int, shop_id: int | None) -> bool:
        async with self.Session() as session:
            return await is_blocked_in_shop(session, customer_id, shop_id)

    async def _records(self) -> list[str]:
        from app.db.models import ShopStaffAction

        async with self.Session() as session:
            return [r.action for r in (await session.scalars(select(ShopStaffAction).order_by(ShopStaffAction.id))).all()]


class ShopBlockTests(BlockDbHarness):
    async def test_block_is_per_shop_and_never_account_wide(self):
        from app.db.models import BotUser

        self.assertTrue(await self._set(_reseller(SHOP_A), 103, True))
        self.assertTrue(await self._blocked(103, SHOP_A))
        self.assertFalse(await self._blocked(103, SHOP_B))
        self.assertFalse(await self._blocked(103, None))
        async with self.Session() as session:
            self.assertFalse((await session.get(BotUser, 103)).is_blocked)

    async def test_attributed_customer_without_a_membership_row_gets_one(self):
        from app.db.models import BotUserShop

        self.assertTrue(await self._set(_reseller(SHOP_A), 101, True))
        async with self.Session() as session:
            rows = (await session.execute(select(func.count()).select_from(BotUserShop).where(BotUserShop.bot_user_id == 101))).scalar()
        self.assertEqual(rows, 1)

    async def test_repeat_and_concurrent_requests_change_once(self):
        results = await asyncio.gather(*(self._set(_reseller(SHOP_A), 101, True) for _ in range(4)), return_exceptions=True)
        self.assertEqual(sum(1 for r in results if r is True), 1)
        self.assertFalse(await self._set(_reseller(SHOP_A), 101, True))
        self.assertEqual(await self._records(), ["customer_block"])
        self.assertTrue(await self._set(_reseller(SHOP_A), 101, False, reason=""))
        self.assertFalse(await self._blocked(101, SHOP_A))
        self.assertEqual(await self._records(), ["customer_block", "customer_unblock"])

    async def test_other_shops_staff_and_unknown_people_are_refused(self):
        for customer_id in (102, SHOP_A, SHOP_B, 104, 99_999):
            with self.subTest(customer_id=customer_id), self.assertRaises(ManualAdjustError):
                await self._set(_reseller(SHOP_A), customer_id, True)
        self.assertEqual(await self._records(), [])

    async def test_block_needs_a_reason_and_the_key(self):
        with self.assertRaises(ManualAdjustError):
            await self._set(_reseller(SHOP_A), 101, True, reason=" ")
        with self.assertRaises(PermissionError):
            await self._set(_reseller(SHOP_A, ["dashboard"]), 101, True)
        self.assertFalse(await self._blocked(101, SHOP_A))

    async def test_shop_bot_notice(self):
        from app.db.models import BotUser

        await self._set(_reseller(SHOP_A), 103, True)
        async with self.Session() as session:
            user = await session.get(BotUser, 103)
            self.assertEqual(await blocked_notice(session, user, is_reseller_bot=True, reseller_owner_id=SHOP_A), BLOCKED_MESSAGE)
            self.assertIsNone(await blocked_notice(session, user, is_reseller_bot=True, reseller_owner_id=SHOP_B))
            self.assertIsNone(await blocked_notice(session, user, is_reseller_bot=False, reseller_owner_id=None))
            user.is_blocked = True
            self.assertEqual(await blocked_notice(session, user, is_reseller_bot=False, reseller_owner_id=None), ACCOUNT_BLOCKED_MESSAGE)

    async def test_campaign_audiences_leave_out_blocked_customers(self):
        from datetime import datetime, timedelta, timezone

        from app.services.campaigns import audience_query

        await self._set(_reseller(SHOP_A), 103, True)
        later = datetime.now(timezone.utc) + timedelta(days=3)
        async with self.Session() as session:
            ids_a = set((await session.scalars(audience_query(shop_id=SHOP_A, audience="inactive", days=1, now=later))).all())
            ids_b = set((await session.scalars(audience_query(shop_id=SHOP_B, audience="inactive", days=1, now=later))).all())
        self.assertNotIn(103, ids_a)
        self.assertIn(103, ids_b)

    async def test_form_context(self):
        from app.db.models import BotUser

        await self._set(_reseller(SHOP_A), 101, True, reason="<b>spam</b>")
        async with self.Session() as session:
            user = await session.get(BotUser, 101)
            ctx = await shop_block_form_context(session, _reseller(SHOP_A), user)
            self.assertEqual((ctx["shop_blocked"], ctx["shop_blocked_reason"]), (True, "<b>spam</b>"))
            self.assertEqual(await shop_block_form_context(session, _reseller(SHOP_A, ["orders"]), user), {"can_block_in_shop": False})


class ShopMiniAppBlockTests(BlockDbHarness):
    async def test_blocked_customer_cannot_load_the_shops_mini_app(self):
        from unittest import mock

        from app.db.models import BotUser
        from app.services import miniapp_auth
        from app.services.miniapp_shop import MiniScope

        await self._set(_reseller(SHOP_A), 103, True)
        scope = MiniScope(shop_id=SHOP_A, bot_token="T")

        async def load(user_id: int, shop_scope):
            async with self.Session() as session:
                user = await session.get(BotUser, user_id)
                with mock.patch.object(miniapp_auth, "assert_miniapp_feature_enabled"), \
                        mock.patch.object(miniapp_auth, "request_scope", return_value=shop_scope), \
                        mock.patch.object(miniapp_auth, "validate_webapp_init_data", return_value={"id": user.telegram_id}), \
                        mock.patch.object(miniapp_auth, "init_data_from_request", return_value="x"), \
                        mock.patch.object(miniapp_auth, "assert_shop_route"), \
                        mock.patch.object(miniapp_auth, "assert_mini_force_join"), \
                        mock.patch.object(miniapp_auth, "assert_mini_terms_entry"):
                    request = mock.MagicMock()
                    return await miniapp_auth.load_mini_user(session, request)

        with self.assertRaises(HTTPException) as caught:
            await load(103, scope)
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (403, BLOCKED_MESSAGE))
        self.assertEqual((await load(101, scope)).id, 101)


class ShopBlockRouteTests(BlockDbHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.shop_customer_block_pages import register_shop_customer_block_pages

        self.staff = _reseller(SHOP_A)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_shop_customer_block_pages(app, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_route(self):
        ok = await self.client.post("/users/101/shop-block", data={"action": "block", "reason": "spam"})
        self.assertIn("ok=", ok.headers["location"])
        self.assertIn("err=", (await self.client.post("/users/102/shop-block", data={"action": "block", "reason": "spam"})).headers["location"])
        self.assertIn("err=", (await self.client.post("/users/101/shop-block", data={"action": "drop"})).headers["location"])
        self.staff = _reseller(SHOP_A, ["dashboard"])
        self.assertEqual((await self.client.post("/users/101/shop-block", data={"action": "unblock"})).headers["location"], "/home")
        self.assertTrue(await self._blocked(101, SHOP_A))


class ShopBlockMigrationTests(unittest.TestCase):
    def test_upgrade_twice_keeps_partial_unique_indexes_and_downgrade_keeps_rows(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("shop_block", ROOT / "alembic/versions/0081_shop_customer_block.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            conn.execute(sa.text("CREATE TABLE bot_user_shops (id INTEGER PRIMARY KEY, bot_user_id INTEGER NOT NULL, shop_id INTEGER, created_at TIMESTAMP)"))
            conn.execute(sa.text("CREATE UNIQUE INDEX uq_bot_user_shops_shop ON bot_user_shops (bot_user_id, shop_id) WHERE shop_id IS NOT NULL"))
            conn.execute(sa.text("INSERT INTO bot_user_shops (bot_user_id, shop_id) VALUES (1, 5)"))
            migration.upgrade()
            migration.upgrade()
            self.assertEqual(conn.execute(sa.text("SELECT is_blocked FROM bot_user_shops")).scalar(), 0)
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(sa.text("INSERT INTO bot_user_shops (bot_user_id, shop_id) VALUES (1, 5)"))
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            migration.downgrade()
            columns = {c["name"] for c in sa.inspect(conn).get_columns("bot_user_shops")}
            self.assertNotIn("is_blocked", columns)
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM bot_user_shops")).scalar(), 1)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
