"""Regression test for a cross-shop leak in ``suggest_receipt_matches``.

Previously, when called with a reseller's ``reseller_id``, the tenant filter
branch contained a no-op ``pass`` instead of ``continue`` (and the query had
no shop filter at all), so a reseller viewing pending-payment "auto match"
suggestions on the finance page would see payment id/order id/amount/user id
of *other* shops' (and the platform's) pending payments. This must never
regress.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class ReceiptMatchTenantIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _make_user(self, session, *, tid: int, reseller_id: int | None = None):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tid,
            username=f"u{tid}",
            full_name="کاربر تست",
            role=Role.USER.value,
            referral_code=f"T{tid}",
            reseller_id=reseller_id,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def _make_order(self, session, *, user_id: int, reseller_id: int | None, amount: int):
        from app.db.models import Order

        o = Order(user_id=user_id, reseller_id=reseller_id, amount=amount)
        session.add(o)
        await session.commit()
        await session.refresh(o)
        return o

    async def _make_payment(
        self,
        session,
        *,
        user_id: int,
        amount: int,
        order_id: int | None = None,
        is_wallet_topup: bool = False,
    ):
        from app.db.models import Payment, PaymentStatus

        p = Payment(
            user_id=user_id,
            amount=amount,
            order_id=order_id,
            status=PaymentStatus.PENDING.value,
            is_wallet_topup=is_wallet_topup,
            created_at=datetime.now(timezone.utc),
        )
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p

    async def test_reseller_never_sees_other_shops_pending_payments(self):
        from app.services.receipt_matching import suggest_receipt_matches

        async with self.Session() as session:
            shop_a_owner = await self._make_user(session, tid=1001)
            shop_b_owner = await self._make_user(session, tid=1002)
            buyer_a = await self._make_user(session, tid=2001, reseller_id=shop_a_owner.id)
            buyer_b = await self._make_user(session, tid=2002, reseller_id=shop_b_owner.id)

            order_a = await self._make_order(
                session, user_id=buyer_a.id, reseller_id=shop_a_owner.id, amount=50000
            )
            order_b = await self._make_order(
                session, user_id=buyer_b.id, reseller_id=shop_b_owner.id, amount=50000
            )

            seed = await self._make_payment(
                session, user_id=buyer_a.id, amount=50000, order_id=order_a.id
            )
            other_shop_payment = await self._make_payment(
                session, user_id=buyer_b.id, amount=50000, order_id=order_b.id
            )

            matches = await suggest_receipt_matches(
                session, payment=seed, reseller_id=shop_a_owner.id
            )

            leaked_ids = {m["id"] for m in matches}
            self.assertNotIn(
                other_shop_payment.id,
                leaked_ids,
                "reseller must never see another shop's pending payment as a match",
            )

    async def test_reseller_sees_own_shop_matches(self):
        from app.services.receipt_matching import suggest_receipt_matches

        async with self.Session() as session:
            shop_owner = await self._make_user(session, tid=1003)
            buyer1 = await self._make_user(session, tid=3001, reseller_id=shop_owner.id)
            buyer2 = await self._make_user(session, tid=3002, reseller_id=shop_owner.id)

            order1 = await self._make_order(
                session, user_id=buyer1.id, reseller_id=shop_owner.id, amount=70000
            )
            order2 = await self._make_order(
                session, user_id=buyer2.id, reseller_id=shop_owner.id, amount=70000
            )

            seed = await self._make_payment(
                session, user_id=buyer1.id, amount=70000, order_id=order1.id
            )
            same_shop_payment = await self._make_payment(
                session, user_id=buyer2.id, amount=70000, order_id=order2.id
            )

            matches = await suggest_receipt_matches(
                session, payment=seed, reseller_id=shop_owner.id
            )

            matched_ids = {m["id"] for m in matches}
            self.assertIn(same_shop_payment.id, matched_ids)

    async def test_platform_scope_excludes_reseller_shop_payments(self):
        from app.services.receipt_matching import suggest_receipt_matches

        async with self.Session() as session:
            shop_owner = await self._make_user(session, tid=1004)
            shop_buyer = await self._make_user(session, tid=4001, reseller_id=shop_owner.id)
            platform_buyer = await self._make_user(session, tid=4002)

            shop_order = await self._make_order(
                session, user_id=shop_buyer.id, reseller_id=shop_owner.id, amount=90000
            )
            platform_order = await self._make_order(
                session, user_id=platform_buyer.id, reseller_id=None, amount=90000
            )

            seed = await self._make_payment(
                session, user_id=platform_buyer.id, amount=90000, order_id=platform_order.id
            )
            shop_payment = await self._make_payment(
                session, user_id=shop_buyer.id, amount=90000, order_id=shop_order.id
            )

            matches = await suggest_receipt_matches(session, payment=seed, reseller_id=None)

            leaked_ids = {m["id"] for m in matches}
            self.assertNotIn(shop_payment.id, leaked_ids)


if __name__ == "__main__":
    unittest.main()
