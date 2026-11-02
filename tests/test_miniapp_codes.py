"""Mini App codes: discounted checkout, gift-code wallet credit, isolation and guessing limits."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_codes import register_miniapp_codes
from app.db.models import Base, BotUser, ChargeCode, DiscountCode, Order, Plan, WalletTransaction
from app.services import discount_checkout
from app.services.discount_checkout import CODE_FAILURE_LIMIT, OPEN_CHECKOUT_LIMIT
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id


class MiniAppCodesTestCase(unittest.IsolatedAsyncioTestCase):
    caller_id = 1

    async def asyncSetUp(self) -> None:
        # A file database so concurrent requests each get their own session and connection.
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'codes.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="a", role="user", wallet_balance=100_000),
            BotUser(id=2, telegram_id=102, referral_code="b", role="user", wallet_balance=100_000),
            BotUser(id=4, telegram_id=104, referral_code="c", role="admin"),
            Plan(id=1, name="monthly", price=100_000, duration_days=30, data_limit_gb=50),
            DiscountCode(id=1, code="OFF20", percent=20, max_uses=5),
            DiscountCode(id=2, code="ONCE", percent=50, max_uses=1),
            ChargeCode(id=1, code="GIFT50", amount=50_000, kind="wallet"),
            ChargeCode(id=2, code="SHOPGIFT", amount=50_000, kind="wallet", reseller_id=3),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        discount_checkout._failures.clear()
        self.settings = {"pay_wallet_enabled": "1"}

        async def load_user(session, _request):
            return await session.scalar(select(BotUser).where(BotUser.id == self.caller_id))

        async def settings(*_args, **_kwargs):
            return dict(self.settings)

        self.patches = [
            patch("app.api.miniapp_codes.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()),
            patch("app.api.miniapp_pages.get_all_settings", AsyncMock(side_effect=settings)),
            # Delivery to the panel is not under test: stop once the wallet is debited.
            patch("app.services.orders._resume_paid_wallet_order", AsyncMock(side_effect=lambda s, o, u: o)),
            patch("app.services.miniapp_auth.is_bot_platform_admin", side_effect=lambda u: u.role == "admin"),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            async with self.Session() as session:
                yield session

        register_miniapp_codes(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def checkout(self, code="OFF20", **extra):
        return await self.client.post("/api/mini/checkout", json={"plan_id": 1, "price": 100_000, "code": code, **extra})

    async def wallet(self, user_id: int = 1) -> int:
        self.session.expire_all()
        return int((await self.session.get(BotUser, user_id)).wallet_balance or 0)

    async def discount(self, code_id: int) -> DiscountCode:
        self.session.expire_all()
        return await self.session.get(DiscountCode, code_id)


class DiscountCheckoutTests(MiniAppCodesTestCase):
    async def test_discount_is_shown_before_paying_and_charged_once(self) -> None:
        response = await self.checkout()
        self.assertEqual(response.status_code, 200, response.text)
        checkout = response.json()["checkout"]
        self.assertEqual((checkout["amount"], checkout["original_amount"], checkout["discount"]), (80_000, 100_000, 20_000))
        self.assertEqual(await self.wallet(), 100_000, "nothing is paid before the customer confirms")
        order_id = checkout["order_id"]
        pay = await self.client.post(f"/api/mini/checkout/{order_id}/pay")
        self.assertEqual(pay.status_code, 200, pay.text)
        self.assertEqual(await self.wallet(), 20_000)
        again = await self.client.post(f"/api/mini/checkout/{order_id}/pay")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(await self.wallet(), 20_000, "a repeated pay never debits twice")
        debits = await self.session.scalar(select(func.count()).select_from(WalletTransaction).where(WalletTransaction.user_id == 1))
        self.assertEqual(debits, 1)
        self.assertEqual((await self.discount(1)).used_count, 1)

    async def test_concurrent_pays_debit_once(self) -> None:
        order_id = (await self.checkout()).json()["checkout"]["order_id"]
        results = await asyncio.gather(*(self.client.post(f"/api/mini/checkout/{order_id}/pay") for _ in range(3)))
        self.assertTrue(any(r.status_code == 200 for r in results))
        self.assertEqual(await self.wallet(), 20_000)

    async def test_rejected_code_cancels_its_order_and_frees_nothing_else(self) -> None:
        response = await self.checkout(code="NOPE")
        self.assertEqual(response.status_code, 400)
        orders = list(await self.session.scalars(select(Order)))
        self.assertEqual([o.status for o in orders], ["cancelled"])
        self.assertEqual(await self.wallet(), 100_000)

    async def test_cancel_releases_the_code_use(self) -> None:
        order_id = (await self.checkout(code="ONCE")).json()["checkout"]["order_id"]
        self.assertEqual((await self.discount(2)).used_count, 1)
        self.assertEqual((await self.checkout(code="ONCE")).status_code, 400, "a single-use code is reserved")
        cancel = await self.client.post(f"/api/mini/checkout/{order_id}/cancel")
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual((await self.discount(2)).used_count, 0)
        self.assertEqual((await self.checkout(code="ONCE")).status_code, 200)

    async def test_another_customer_cannot_pay_or_cancel_the_order(self) -> None:
        order_id = (await self.checkout()).json()["checkout"]["order_id"]
        self.caller_id = 2
        self.assertEqual((await self.client.post(f"/api/mini/checkout/{order_id}/pay")).status_code, 400)
        self.assertEqual((await self.client.post(f"/api/mini/checkout/{order_id}/cancel")).status_code, 400)
        self.assertEqual(await self.wallet(2), 100_000)
        self.assertEqual(await self.wallet(1), 100_000)
        self.assertEqual((await self.session.get(Order, order_id)).status, "pending")

    async def test_shop_orders_are_out_of_reach(self) -> None:
        self.session.add(Order(id=70, user_id=1, plan_id=1, amount=50_000, status="pending", reseller_id=3))
        await self.session.commit()
        self.assertEqual((await self.client.post("/api/mini/checkout/70/pay")).status_code, 400)
        self.assertEqual((await self.client.post("/api/mini/checkout/70/cancel")).status_code, 400)
        self.session.expire_all()
        self.assertEqual((await self.session.get(Order, 70)).status, "pending")

    async def test_insufficient_wallet_does_not_pay(self) -> None:
        order_id = (await self.checkout()).json()["checkout"]["order_id"]
        user = await self.session.get(BotUser, 1)
        user.wallet_balance = 79_999
        await self.session.commit()
        self.assertEqual((await self.client.post(f"/api/mini/checkout/{order_id}/pay")).status_code, 400)
        self.assertEqual(await self.wallet(), 79_999)

    async def test_bad_input_and_disabled_wallet_pay(self) -> None:
        for body in ({"code": ""}, {"code": "x" * 65}, {"code": 5}, {"plan_id": True}, {"plan_id": "1"}, {"plan_id": 99}, {"price": 1}):
            with self.subTest(body=body):
                self.assertEqual((await self.checkout(**body)).status_code, 400)
        self.settings["pay_wallet_enabled"] = "0"
        self.assertEqual((await self.checkout()).status_code, 403)
        self.assertEqual(await self.wallet(), 100_000)

    async def test_open_discounted_orders_are_limited(self) -> None:
        for _ in range(OPEN_CHECKOUT_LIMIT):
            self.assertEqual((await self.checkout()).status_code, 200)
        self.assertEqual((await self.checkout()).status_code, 400)

    async def test_admin_is_denied(self) -> None:
        self.caller_id = 4
        self.assertEqual((await self.checkout()).status_code, 403)


class GiftCodeTests(MiniAppCodesTestCase):
    async def redeem(self, code):
        return await self.client.post("/api/mini/wallet/redeem", json={"code": code})

    async def test_gift_code_credits_the_platform_wallet_once(self) -> None:
        response = await self.redeem(" gift50 ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["amount"], 50_000)
        self.assertEqual(await self.wallet(), 150_000)
        self.assertEqual((await self.redeem("GIFT50")).status_code, 400)
        self.assertEqual(await self.wallet(), 150_000)

    async def test_concurrent_redeems_credit_once(self) -> None:
        results = await asyncio.gather(*(self.redeem("GIFT50") for _ in range(3)))
        codes = [r.status_code for r in results]
        self.assertLessEqual(codes.count(200), 1)
        self.assertTrue(all(c in (200, 400) for c in codes), codes)
        if 200 not in codes:
            # Every try lost the database race; one more succeeds.
            self.assertEqual((await self.redeem("GIFT50")).status_code, 200)
        self.assertEqual(await self.wallet(), 150_000)
        self.assertEqual((await self.redeem("GIFT50")).status_code, 400)
        self.assertEqual(await self.wallet(), 150_000)

    async def test_shop_code_does_not_work_on_the_platform(self) -> None:
        self.assertEqual((await self.redeem("SHOPGIFT")).status_code, 400)
        self.assertEqual(await self.wallet(), 100_000)

    async def test_failed_attempts_are_limited_per_customer(self) -> None:
        for i in range(CODE_FAILURE_LIMIT):
            self.assertEqual((await self.redeem(f"GUESS{i}")).status_code, 400)
        blocked = await self.redeem("GIFT50")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("تلاش", blocked.json()["detail"])
        self.assertEqual(await self.wallet(), 100_000, "even a valid code waits out the limit")
        self.caller_id = 2
        self.assertEqual((await self.redeem("GIFT50")).status_code, 200, "the limit is per customer")


if __name__ == "__main__":
    unittest.main()
