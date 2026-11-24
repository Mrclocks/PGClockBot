"""The order discount ledger records each reduction once and releases it with the unpaid order."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, DiscountCode, LoyaltyDiscountEntitlement, Order, OrderDiscount, Plan
from app.services.charge_codes import create_charge_code
from app.services.order_discounts import (
    SOURCE_CODE,
    SOURCE_GIFT,
    SOURCE_LOYALTY,
    live_order_discounts,
    record_order_discount,
    release_order_discounts,
)
from app.services.orders import (
    _release_order_discount,
    apply_discount_to_order,
    cancel_order,
    release_unpaid_order_holds,
)
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP = 3


class OrderDiscountLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=1, referral_code="one"),
                BotUser(id=2, telegram_id=2, referral_code="two"),
                BotUser(id=SHOP, telegram_id=3, referral_code="shop"),
                Plan(id=1, name="paid", price=200_000, duration_days=30),
                DiscountCode(code="LEGACY10", percent=10, max_uses=5, used_count=0, is_active=True),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def order(self, session, *, user_id: int = 1, shop: int | None = None, status: str = "pending") -> Order:
        row = Order(user_id=user_id, amount=200_000, reseller_id=shop, status=status, plan_id=1)
        session.add(row)
        await session.commit()
        return row

    async def test_gift_code_records_one_gift_line(self) -> None:
        async with self.Session() as session:
            gift = await create_charge_code(session, kind="discount", percent=20, max_uses=None)
            order = await apply_discount_to_order(session, await self.order(session), gift.code)
            lines = await live_order_discounts(session, order.id)
        self.assertEqual([(line.source, line.amount, line.shop_key, line.user_id) for line in lines],
                         [(SOURCE_GIFT, 40_000, 0, 1)])

    async def test_legacy_code_records_code_line(self) -> None:
        async with self.Session() as session:
            order = await apply_discount_to_order(session, await self.order(session), "legacy10")
            lines = await live_order_discounts(session, order.id)
        self.assertEqual([(line.source, line.source_ref, line.amount) for line in lines],
                         [(SOURCE_CODE, "LEGACY10", 20_000)])

    async def test_line_takes_the_shop_of_the_order(self) -> None:
        async with self.Session() as session:
            order = await self.order(session, shop=SHOP)
            await record_order_discount(session, order, source=SOURCE_CODE, amount=5_000)
            await session.commit()
            line = (await live_order_discounts(session, order.id))[0]
        self.assertEqual(line.shop_key, SHOP)

    async def test_repeated_record_does_not_add_a_second_line(self) -> None:
        async with self.Session() as session:
            order = await self.order(session)
            self.assertTrue(await record_order_discount(session, order, source=SOURCE_CODE, amount=5_000))
            self.assertFalse(await record_order_discount(session, order, source=SOURCE_CODE, amount=9_000))
            await session.commit()
            count = await session.scalar(select(sa.func.count()).select_from(OrderDiscount))
        self.assertEqual(count, 1)

    async def test_record_refuses_unknown_source_and_skips_non_positive_amounts(self) -> None:
        async with self.Session() as session:
            order = await self.order(session)
            with self.assertRaises(ValueError):
                await record_order_discount(session, order, source="bogus", amount=5_000)
            for amount in (0, -1, True):
                self.assertFalse(await record_order_discount(session, order, source=SOURCE_CODE, amount=amount))

    async def test_cancel_releases_the_line_and_the_code_use(self) -> None:
        async with self.Session() as session:
            order = await apply_discount_to_order(session, await self.order(session), "LEGACY10")
            await cancel_order(session, order, note="test")
            await session.commit()
            self.assertEqual(await live_order_discounts(session, order.id), [])
            used = await session.scalar(select(DiscountCode.used_count).where(DiscountCode.code == "LEGACY10"))
        self.assertEqual(used, 0)

    async def test_release_twice_changes_nothing_the_second_time(self) -> None:
        async with self.Session() as session:
            order = await apply_discount_to_order(session, await self.order(session), "LEGACY10")
            self.assertEqual(await release_order_discounts(session, order.id), 1)
            self.assertEqual(await release_order_discounts(session, order.id), 0)

    async def test_cleanup_release_frees_the_line(self) -> None:
        async with self.Session() as session:
            order = await apply_discount_to_order(session, await self.order(session), "LEGACY10")
            order.status = "cancelled"
            await release_unpaid_order_holds(session, order, reason="test")
            await session.commit()
            self.assertEqual(await live_order_discounts(session, order.id), [])

    async def test_rejected_attempt_releases_line_and_restores_price(self) -> None:
        async with self.Session() as session:
            order = await apply_discount_to_order(session, await self.order(session), "LEGACY10")
            await _release_order_discount(session, order)
            await session.commit()
            self.assertEqual(order.amount, 200_000)
            self.assertEqual(await live_order_discounts(session, order.id), [])

    async def test_release_of_one_order_leaves_another_customer_line(self) -> None:
        async with self.Session() as session:
            first = await apply_discount_to_order(session, await self.order(session), "LEGACY10")
            second = await apply_discount_to_order(session, await self.order(session, user_id=2), "LEGACY10")
            await cancel_order(session, first, note="test")
            await session.commit()
            self.assertEqual(len(await live_order_discounts(session, second.id)), 1)

    async def test_loyalty_entitlement_records_loyalty_line(self) -> None:
        async with self.Session() as session:
            session.add(LoyaltyDiscountEntitlement(user_id=1, code="LOYTEST1", percent=15, status="available"))
            await session.commit()
            order = await apply_discount_to_order(session, await self.order(session), "LOYTEST1")
            lines = await live_order_discounts(session, order.id)
        self.assertEqual([(line.source, line.amount) for line in lines], [(SOURCE_LOYALTY, 30_000)])


class OrderDiscountMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keeps_orders(self) -> None:
        spec = importlib.util.spec_from_file_location("ledger", ROOT / "alembic/versions/0127_order_discounts.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO orders VALUES (7)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                insert = text("INSERT INTO order_discounts (order_id, user_id, source, amount) VALUES (7, 1, :s, 10)")
                conn.execute(insert, {"s": "code"})
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(insert, {"s": "code"})
                indexes = {index["name"] for index in sa.inspect(conn).get_indexes("order_discounts")}
                self.assertTrue({"ix_order_discounts_shop_source_ref", "ix_order_discounts_shop_user"} <= indexes)
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("order_discounts"))
                self.assertEqual(conn.execute(text("SELECT count(*) FROM orders")).scalar(), 1)
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
