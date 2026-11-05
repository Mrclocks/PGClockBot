"""A shop's Mini App: required service name and staff free checkout, with the bot's rules."""

from __future__ import annotations

import json

from unittest.mock import AsyncMock, patch

from sqlalchemy import select, update

from app.db.models import BotUser, Order, OrderStatus, Payment, ResellerProfile
from tests.test_miniapp_shop_purchase import SHOP_PLAN, ShopPurchaseTests
from tests.test_miniapp_shop_readonly import CUSTOMER, KEY, PERMS, SHOP
from tests.test_miniapp_shop_scope import SHOP_TOKEN, signed

# The fixture's shop owner (BotUser SHOP) in Telegram.
OWNER_TELEGRAM_ID = 5005


class MiniCheckoutOptionsTests(ShopPurchaseTests):
    """Built on the shop purchase fixture (plans, purses, a stubbed panel delivery)."""

    async def grant(self, extra: str, *, staff: bool = False) -> None:
        """The fixture's shop permissions plus ``extra`` (comma separated)."""
        permissions = json.dumps([*json.loads(PERMS), *(k for k in extra.split(",") if k)])
        async with self.Session() as session:
            await session.execute(
                update(ResellerProfile)
                .where(ResellerProfile.user_id == SHOP)
                .values(
                    web_permissions=permissions,
                    bot_permissions=permissions,
                    bot_admin_ids=str(CUSTOMER) if staff else None,
                )
            )
            await session.commit()

    async def as_owner(self, method: str, path: str, body: dict | None = None):
        """The shop owner opening their own shop's Mini App."""
        headers = {"X-Telegram-Init-Data": signed(SHOP_TOKEN, OWNER_TELEGRAM_ID), "X-Mini-Shop": KEY}
        return await self.client.request(method, path, json=body, headers=headers)

    async def orders(self) -> list[Order]:
        async with self.Session() as session:
            return list((await session.scalars(select(Order).order_by(Order.id))).all())

    async def test_me_reports_the_checkout_options(self) -> None:
        await self.grant("custom_service_name,free_checkout")
        options = (await self.as_owner("GET", "/api/mini/me")).json()["customer"]["checkout"]
        self.assertEqual(options, {"service_name_required": True, "staff_free": True})

        await self.grant("")
        options = (await self.as_owner("GET", "/api/mini/me")).json()["customer"]["checkout"]
        self.assertEqual(options, {"service_name_required": False, "staff_free": False})

    async def test_a_shop_bot_admin_is_not_offered_free_checkout(self) -> None:
        await self.grant("free_checkout", staff=True)
        options = (await self.shop_get("/api/mini/me")).json()["customer"]["checkout"]
        self.assertFalse(options["staff_free"])

    async def test_a_required_name_is_checked_before_any_order_exists(self) -> None:
        await self.grant("custom_service_name")
        for body in ({"plan_id": SHOP_PLAN}, {"plan_id": SHOP_PLAN, "service_name": "علی"}):
            with self.subTest(body=body):
                self.assertEqual((await self.post("/api/mini/buy", body)).status_code, 400)
        self.assertEqual(await self.orders(), [])
        self.assertEqual(await self.purses(), (50_000, 7_000))

    async def test_the_name_is_stored_on_the_paid_order(self) -> None:
        await self.grant("custom_service_name")
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000, "service_name": "Ali"})
        self.assertEqual(response.status_code, 200, response.text)
        [order] = await self.orders()
        self.assertEqual(order.requested_username, "ali")

    async def test_a_name_is_ignored_where_it_is_not_required(self) -> None:
        await self.grant("")
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000, "service_name": "ali"})
        self.assertEqual(response.status_code, 200, response.text)
        [order] = await self.orders()
        self.assertIsNone(order.requested_username)

    async def test_a_customer_cannot_take_an_order_free(self) -> None:
        await self.grant("free_checkout")
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "mode": "staff_free"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(await self.orders(), [])

    async def test_a_shop_bot_admin_cannot_take_an_order_free(self) -> None:
        await self.grant("free_checkout", staff=True)
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "mode": "staff_free"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(await self.orders(), [])

    async def test_the_shop_owner_takes_their_order_free_without_touching_a_purse(self) -> None:
        await self.grant("free_checkout")
        deliver = AsyncMock(side_effect=lambda _session, order, **_kw: order)
        with patch("app.services.staff_free_checkout.deliver_order", deliver):
            response = await self.as_owner("POST", "/api/mini/buy", {"plan_id": SHOP_PLAN, "mode": "staff_free"})
        self.assertEqual(response.status_code, 200, response.text)
        [order] = await self.orders()
        self.assertEqual(
            (order.user_id, order.amount, order.payment_method, order.status),
            (SHOP, 0, "staff_free", OrderStatus.PAID.value),
        )
        deliver.assert_awaited_once()
        async with self.Session() as session:
            self.assertEqual(int((await session.get(BotUser, SHOP)).wallet_balance), 0)

    async def test_a_failed_free_delivery_cancels_the_order_and_says_so(self) -> None:
        await self.grant("free_checkout")
        with patch("app.services.staff_free_checkout.deliver_order", AsyncMock(side_effect=ValueError("no group"))):
            response = await self.as_owner("POST", "/api/mini/buy", {"plan_id": SHOP_PLAN, "mode": "staff_free"})
        self.assertEqual(response.status_code, 502)
        [order] = await self.orders()
        self.assertEqual(order.status, OrderStatus.CANCELLED.value)

    async def test_the_discounted_checkout_needs_the_name_before_paying(self) -> None:
        await self.grant("custom_service_name")
        async with self.Session() as session:
            order = Order(user_id=10, plan_id=SHOP_PLAN, reseller_id=SHOP, amount=4_000, discount_amount=1_000)
            session.add(order)
            await session.commit()
            order_id = order.id
        pay = AsyncMock(side_effect=lambda session, **kw: order)
        with patch("app.api.miniapp_codes.pay_checkout", pay):
            missing = await self.post(f"/api/mini/checkout/{order_id}/pay", {})
            named = await self.post(f"/api/mini/checkout/{order_id}/pay", {"service_name": "ali"})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(named.status_code, 200, named.text)
        self.assertEqual(pay.await_count, 1)
        [stored] = await self.orders()
        self.assertEqual(stored.requested_username, "ali")
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(Payment.id).limit(1)), None)

