"""Routine cleanup preserves surprise-price protection; intentional user deletion removes it."""
from __future__ import annotations

import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base, BotUser, Order, Payment, RandomDiscount, RandomDiscountDraw, RandomDiscountReservation,
)
from app.services.order_purge import (
    PurgeRules, _purge_one_order, auto_purge_all_shops, count_purgeable, run_purge, save_rules,
)
from app.services.user_purge import purge_user_references
from app.services.users import clear_settings_cache


def _enable_foreign_keys(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class RandomDiscountPurgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.tmp.name}/purge.db")
        event.listen(self.engine.sync_engine, "connect", _enable_foreign_keys)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(days=2)
        self.session.add_all([
            BotUser(id=user_id, telegram_id=9000 + user_id, referral_code=f"purge{user_id}")
            for user_id in (1, 2, 3, 4)
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()
        clear_settings_cache()

    async def fixture(self, *, shop_id: int = 2, user_id: int = 1) -> tuple[Order, Payment, RandomDiscountDraw, RandomDiscountReservation]:
        policy = RandomDiscount(
            shop_key=shop_id, reseller_id=shop_id, title="تخفیف", mode="automatic", reduction_kind="amount",
            minimum=10, maximum=20, step=1, window_minutes=10080, request_key=secrets.token_hex(16),
        )
        self.session.add(policy)
        await self.session.flush()
        order = Order(
            user_id=user_id, reseller_id=shop_id, amount=990, discount_amount=10, random_discount_amount=10,
            random_discount_id=policy.id, status="cancelled", created_at=self.old, updated_at=self.old,
        )
        self.session.add(order)
        await self.session.flush()
        payment = Payment(user_id=user_id, order_id=order.id, amount=990, status="rejected", created_at=self.old)
        draw = RandomDiscountDraw(
            shop_key=shop_id, reseller_id=shop_id, policy_id=policy.id, order_id=order.id,
            user_id=user_id, base_amount=1000, discount_amount=10, final_amount=990,
            created_at=self.old, protected_until=self.now + timedelta(days=5),
        )
        reservation = RandomDiscountReservation(
            shop_key=shop_id, final_amount=990, order_id=order.id, protected_until=self.now + timedelta(days=5),
        )
        self.session.add_all([payment, draw, reservation])
        await self.session.commit()
        return order, payment, draw, reservation

    async def test_preview_and_run_keep_draws_and_payments_until_protection_expires(self) -> None:
        order, payment, draw, reservation = await self.fixture()
        tags = frozenset({"order:cancelled", "payment:rejected"})
        preview = await count_purgeable(self.session, shop_id=2, tags=tags, days=1, now=self.now)
        done = await run_purge(self.session, shop_id=2, tags=tags, days=1, now=self.now)
        self.assertEqual((preview.total, done.total), (0, 0))
        for model, row_id in ((Order, order.id), (Payment, payment.id), (RandomDiscountDraw, draw.id), (RandomDiscountReservation, reservation.id)):
            self.assertIsNotNone(await self.session.get(model, row_id))
        later = self.now + timedelta(days=6)
        preview = await count_purgeable(self.session, shop_id=2, tags=tags, days=1, now=later)
        done = await run_purge(self.session, shop_id=2, tags=tags, days=1, now=later)
        self.assertEqual((preview.orders, preview.payments), (1, 1))
        self.assertEqual((done.orders, done.payments), (1, 1))
        for model in (Order, Payment, RandomDiscountDraw, RandomDiscountReservation):
            self.assertEqual(await self.session.scalar(select(func.count()).select_from(model)), 0)

    async def test_either_draw_or_amount_reservation_prevents_deletion(self) -> None:
        order, _, draw, reservation = await self.fixture()
        draw.protected_until = self.old
        await self.session.commit()
        tags = frozenset({"order:cancelled"})
        self.assertEqual((await count_purgeable(self.session, shop_id=2, tags=tags, days=1)).orders, 0)
        draw.protected_until = self.now + timedelta(days=5)
        reservation.protected_until = self.old
        await self.session.commit()
        self.assertEqual((await count_purgeable(self.session, shop_id=2, tags=tags, days=1)).orders, 0)
        self.assertIsNotNone(await self.session.get(Order, order.id))

    async def test_mutation_rechecks_protection_and_shop(self) -> None:
        order, _, _, _ = await self.fixture()
        order_id = order.id
        for shop_id in (2, 3):
            result = await _purge_one_order(self.session, order_id, shop_id=shop_id, statuses=["cancelled"])
            self.assertIsNone(result)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(Order)), 1)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 1)

    async def test_automatic_cleanup_preserves_active_protection(self) -> None:
        await self.fixture()
        await save_rules(self.session, shop_id=2, rules=PurgeRules(
            tags=frozenset({"order:cancelled", "payment:rejected"}), auto=True, days=1,
        ))
        self.assertEqual(await auto_purge_all_shops(self.session), 0)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(Order)), 1)

    async def test_permanent_shop_deletion_removes_its_policies_without_promoting_them(self) -> None:
        await self.fixture(shop_id=2)
        await self.fixture(shop_id=3, user_id=4)
        await purge_user_references(self.session, 2)
        await self.session.execute(delete(BotUser).where(BotUser.id == 2))
        await self.session.commit()
        self.assertEqual(list((await self.session.scalars(select(RandomDiscount.shop_key))).all()), [3])
        self.assertEqual(list((await self.session.scalars(select(RandomDiscountDraw.shop_key))).all()), [3])
        self.assertEqual(list((await self.session.scalars(select(RandomDiscountReservation.shop_key))).all()), [3])
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscount).where(RandomDiscount.reseller_id.is_(None))), 0)
        self.assertIsNotNone(await self.session.get(BotUser, 1))
        self.assertIsNotNone(await self.session.get(BotUser, 3))

    async def test_permanent_customer_deletion_removes_its_draws_without_deleting_shop_policy(self) -> None:
        await self.fixture()
        await purge_user_references(self.session, 1)
        await self.session.execute(delete(BotUser).where(BotUser.id == 1))
        await self.session.commit()
        for model in (Order, Payment, RandomDiscountDraw, RandomDiscountReservation):
            self.assertEqual(await self.session.scalar(select(func.count()).select_from(model)), 0)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscount)), 1)
