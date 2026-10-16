"""The wheel counters of a customer are kept per shop; the migration allows one row per pair."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, LuckyWheelUserState
from app.services.lucky_wheel import _get_or_create_state
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

ROOT = Path(__file__).resolve().parents[1]
SHOP_ID, CUSTOMER = 5, 7


class WheelStatePerShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'w.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=CUSTOMER, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()

    async def state(self, shop: int | None) -> LuckyWheelUserState:
        token = set_shop_reseller_id(shop)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER)
                state = await _get_or_create_state(session, customer)
                await session.commit()
                return state
        finally:
            reset_shop_reseller_id(token)

    async def test_each_shops_wheel_has_its_own_counters(self):
        platform = await self.state(None)
        shop = await self.state(SHOP_ID)
        self.assertNotEqual(platform.id, shop.id)
        self.assertEqual((platform.reseller_id, shop.reseller_id), (None, SHOP_ID))
        self.assertEqual((await self.state(None)).id, platform.id)
        self.assertEqual((await self.state(SHOP_ID)).id, shop.id)

    async def test_free_spins_of_one_wheel_are_not_in_another(self):
        async with self.Session() as session:
            session.add(LuckyWheelUserState(user_id=CUSTOMER, reseller_id=SHOP_ID, free_spins_balance=3))
            await session.commit()
        self.assertEqual((await self.state(SHOP_ID)).free_spins_balance, 3)
        self.assertEqual((await self.state(None)).free_spins_balance, 0)


class WheelStateMigrationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("wheel_state_migration", ROOT / "alembic/versions/0062_wheel_state_per_shop.py")
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE lucky_wheel_user_state (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "reseller_id INTEGER, free_spins_balance INTEGER DEFAULT 0, "
                "CONSTRAINT uq_lucky_wheel_user_state_user UNIQUE (user_id))"
            ))
            conn.execute(sa.text("INSERT INTO lucky_wheel_user_state VALUES (1, 7, 5, 2), (2, 8, NULL, 0)"))

    def run_migration(self, conn, *, down: bool = False):
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def test_a_customer_can_have_one_row_per_shop_and_one_for_the_platform(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            self.run_migration(conn)  # idempotent
            conn.execute(sa.text("INSERT INTO lucky_wheel_user_state VALUES (3, 7, NULL, 0), (4, 7, 6, 0)"))
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(sa.text("INSERT INTO lucky_wheel_user_state VALUES (5, 7, 5, 0)"))
        with self.engine.begin() as conn:
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM lucky_wheel_user_state")).scalar(), 4)

    def test_downgrade_keeps_the_newest_row_per_customer_and_restores_the_constraint(self):
        with self.engine.begin() as conn:
            self.run_migration(conn)
            conn.execute(sa.text("INSERT INTO lucky_wheel_user_state VALUES (3, 7, NULL, 0)"))
            self.run_migration(conn, down=True)
            rows = dict(conn.execute(sa.text("SELECT user_id, id FROM lucky_wheel_user_state")).all())
            self.assertEqual(rows, {7: 3, 8: 2})
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(sa.text("INSERT INTO lucky_wheel_user_state VALUES (9, 7, 5, 0)"))


class WheelStateMigrationOtherNameTests(unittest.TestCase):
    def test_a_per_customer_unique_with_another_name_is_dropped_too(self):
        spec = importlib.util.spec_from_file_location("wheel_state_migration2", ROOT / "alembic/versions/0062_wheel_state_per_shop.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE lucky_wheel_user_state (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "reseller_id INTEGER, CONSTRAINT some_other_name UNIQUE (user_id))"
            ))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
            conn.execute(sa.text("INSERT INTO lucky_wheel_user_state VALUES (1, 7, NULL), (2, 7, 5)"))
