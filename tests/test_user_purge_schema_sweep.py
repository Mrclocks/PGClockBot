"""User deletion must not be blocked by rows the explicit cleanup list misses."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import (
    BotUser,
    CatalogDiscount,
    DeliveryFailure,
    Order,
    Role,
    ServiceAutomation,
    TrialClaim,
    UserService,
)
from app.services.user_purge import purge_user_references
from app.services.users import delete_bot_user
from tests.postgres_support import scoped_async_engine


def _user(telegram_id: int) -> BotUser:
    return BotUser(
        telegram_id=telegram_id,
        username=f"u{telegram_id}",
        full_name=f"user {telegram_id}",
        role=Role.USER.value,
        referral_code=f"R{telegram_id}",
    )


class UserPurgeSchemaSweepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _count(self, session, model) -> int:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)

    async def test_delete_succeeds_with_trial_claim_and_delivery_failure_on_orders(self) -> None:
        async with self.Session() as session:
            user = _user(960001)
            session.add(user)
            await session.flush()
            order = Order(user_id=user.id, amount=1000)
            session.add(order)
            await session.flush()
            session.add(TrialClaim(user_id=user.id, shop_key="platform", order_id=order.id))
            session.add(DeliveryFailure(order_id=order.id, error="x"))
            await session.commit()
            uid = user.id

            with patch("app.services.pasarguard.get_pg", return_value=AsyncMock()):
                await delete_bot_user(session, uid)

            self.assertIsNone(await session.get(BotUser, uid))
            self.assertEqual(await self._count(session, TrialClaim), 0)
            self.assertEqual(await self._count(session, DeliveryFailure), 0)

    async def test_deleting_a_shop_removes_its_discounts_and_automations_only(self) -> None:
        async with self.Session() as session:
            shop = _user(960002)
            customer = _user(960003)
            other_shop = _user(960004)
            session.add_all([shop, customer, other_shop])
            await session.flush()
            service = UserService(bot_user_id=customer.id, pg_username="svc960003")
            session.add(service)
            await session.flush()
            session.add(ServiceAutomation(service_id=service.id, shop_id=shop.id))
            for owner, key in ((shop, "a"), (other_shop, "b")):
                session.add(
                    CatalogDiscount(
                        shop_key=owner.id,
                        reseller_id=owner.id,
                        title="t",
                        scope="all",
                        mode="m",
                        reduction_kind="percent",
                        reduction_value=10,
                        request_key=key,
                    )
                )
            await session.commit()
            shop_id, other_id = shop.id, other_shop.id

            with patch("app.services.pasarguard.get_pg", return_value=AsyncMock()):
                await delete_bot_user(session, shop_id)

            self.assertIsNone(await session.get(BotUser, shop_id))
            remaining = (await session.scalars(select(CatalogDiscount.reseller_id))).all()
            self.assertEqual(remaining, [other_id])
            self.assertEqual(await self._count(session, ServiceAutomation), 0)
            self.assertIsNotNone(await session.get(BotUser, customer.id))
            self.assertEqual(await self._count(session, UserService), 1)

    async def test_sweep_runs_against_every_foreign_key_in_the_schema(self) -> None:
        async with self.Session() as session:
            user = _user(960005)
            session.add(user)
            await session.commit()
            await purge_user_references(session, user.id)
            await session.execute(text("DELETE FROM bot_users WHERE id = :uid"), {"uid": user.id})
            await session.commit()
            self.assertEqual(await self._count(session, BotUser), 0)


if __name__ == "__main__":
    unittest.main()
