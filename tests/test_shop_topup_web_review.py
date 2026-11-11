"""Shop staff review their own shop's wallet top-ups in the web panel, and nobody else's."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment, PaymentStatus, ShopWallet
from app.services.shop_scope import shop_may_review_payment
from tests.test_update_owner_only import RoutesHarness, SHOP_SESSION

SHOP_ID = 5
OTHER_SHOP_ID = 6
TOPUP_AMOUNT = 50_000


class ShopTopupWebReviewTests(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        notify = patch("app.services.reseller_bots.open_notify_bot_for_user", AsyncMock(side_effect=RuntimeError("offline")))
        notify.start()
        self.addCleanup(notify.stop)
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add(BotUser(id=7, telegram_id=107, referral_code="cust7", role="user", reseller_id=SHOP_ID))
            for payment_id, shop in ((41, SHOP_ID), (42, OTHER_SHOP_ID), (43, None)):
                session.add(Payment(
                    id=payment_id, user_id=7, amount=TOPUP_AMOUNT, is_wallet_topup=True, wallet_shop_id=shop,
                    status=PaymentStatus.PENDING.value,
                ))
            await session.commit()

    async def post(self, path: str):
        return await self.call(SHOP_SESSION, "POST", path)

    async def status_of(self, payment_id: int) -> str:
        async with AsyncSession(bind=self.engine) as session:
            return (await session.get(Payment, payment_id)).status

    async def shop_balance(self) -> int:
        async with AsyncSession(bind=self.engine) as session:
            wallet = await session.scalar(
                select(ShopWallet).where(ShopWallet.user_id == 7, ShopWallet.reseller_id == SHOP_ID)
            )
            return int(wallet.balance) if wallet else 0

    def _was_refused(self, response) -> bool:
        return "err" in parse_qs(urlsplit(response.headers["location"]).query)

    async def test_shop_staff_approve_their_own_shop_topup(self):
        response = await self.post("/payments/41/approve")
        self.assertFalse(self._was_refused(response), response.headers["location"])
        self.assertEqual(await self.status_of(41), PaymentStatus.APPROVED.value)
        self.assertEqual(await self.shop_balance(), TOPUP_AMOUNT)

    async def test_shop_staff_reject_their_own_shop_topup(self):
        response = await self.post("/payments/41/reject")
        self.assertFalse(self._was_refused(response), response.headers["location"])
        self.assertEqual(await self.status_of(41), PaymentStatus.REJECTED.value)

    async def test_shop_staff_cannot_touch_another_shops_or_the_platforms_topup(self):
        for payment_id in (42, 43):
            for action in ("approve", "reject"):
                response = await self.post(f"/payments/{payment_id}/{action}")
                self.assertTrue(self._was_refused(response), (payment_id, action))
            self.assertEqual(await self.status_of(payment_id), PaymentStatus.PENDING.value)
        self.assertEqual(await self.shop_balance(), 0)


class ShopMayReviewPaymentTests(unittest.TestCase):
    def test_a_payment_without_an_order_belongs_to_the_platform(self):
        payment = Payment(user_id=7, amount=1, is_wallet_topup=False)
        self.assertFalse(shop_may_review_payment(payment, None, SHOP_ID))

    def test_an_order_payment_belongs_to_the_shop_of_its_order(self):
        payment = Payment(user_id=7, amount=1, is_wallet_topup=False)
        self.assertTrue(shop_may_review_payment(payment, Order(user_id=7, amount=1, reseller_id=SHOP_ID), SHOP_ID))
        self.assertFalse(shop_may_review_payment(payment, Order(user_id=7, amount=1, reseller_id=OTHER_SHOP_ID), SHOP_ID))
        self.assertFalse(shop_may_review_payment(payment, Order(user_id=7, amount=1), SHOP_ID))
