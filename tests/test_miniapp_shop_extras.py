"""A shop's Mini App support desk, codes and club are that shop's only."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.api.miniapp_codes import register_miniapp_codes
from app.api.miniapp_loyalty import register_miniapp_loyalty
from app.api.miniapp_support import register_miniapp_support
from app.db.models import (
    BotUser,
    ChargeCode,
    DiscountCode,
    Order,
    OrderStatus,
    Plan,
    PointsTransaction,
    ShopPoints,
    ShopWallet,
    Ticket,
)
from app.services import discount_checkout
from app.services.users import clear_settings_cache, set_setting
from tests.test_miniapp_loyalty import points_entry, reward
from tests.test_miniapp_shop_readonly import CUSTOMER, KEY, OTHER_SHOP, SHOP, ShopFixture
from tests.test_miniapp_shop_scope import MAIN_TOKEN, SHOP_TOKEN, signed

SHOP_PLAN, PLATFORM_PLAN = 61, 62
PLATFORM_TICKET, OTHER_SHOP_TICKET = 71, 72


class ShopExtrasFixture(ShopFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        discount_checkout._failures.clear()
        async with self.Session() as session:
            session.add_all([
                Plan(id=SHOP_PLAN, name="shop plan", price=5_000, duration_days=30, data_limit_gb=10,
                     owner_reseller_id=SHOP, is_active=True),
                Plan(id=PLATFORM_PLAN, name="platform plan", price=5_000, duration_days=30, data_limit_gb=10,
                     is_active=True),
                ChargeCode(code="SHOPOFF", kind="discount", percent=20, reseller_id=SHOP),
                ChargeCode(code="PLATOFF", kind="discount", percent=20),
                ChargeCode(code="SHOPGIFT", amount=3_000, kind="wallet", reseller_id=SHOP),
                ChargeCode(code="PLATGIFT", amount=3_000, kind="wallet"),
                ChargeCode(code="OTHERGIFT", amount=3_000, kind="wallet", reseller_id=OTHER_SHOP),
                DiscountCode(code="LEGACY50", percent=50),
                Ticket(id=PLATFORM_TICKET, user_id=10, subject="platform ticket"),
                Ticket(id=OTHER_SHOP_TICKET, user_id=10, subject="other shop ticket", reseller_id=OTHER_SHOP),
                reward(81, "wallet_credit", 2_000, 100, reseller_id=SHOP),
                reward(82, "wallet_credit", 2_000, 100),
                ShopPoints(user_id=10, reseller_id=SHOP, balance=300),
                points_entry(91, 10, 300, shop_id=SHOP),
                points_entry(92, 10, 900),
            ])
            (await session.get(BotUser, 10)).points_balance = 900
            await session.commit()
            await set_setting(session, "pay_wallet_enabled", "1", reseller_id=SHOP)
        clear_settings_cache()
        self.opener = AsyncMock(return_value=(SimpleNamespace(session=SimpleNamespace(close=AsyncMock())), False))
        self.new_ticket = AsyncMock()
        for p in (
            patch("app.api.miniapp_support.open_notify_bot_for_user", self.opener),
            patch("app.services.customer_support.notify_new_ticket", self.new_ticket),
            patch("app.services.customer_support.notify_ticket_message", AsyncMock()),
            patch("app.services.orders.fulfill_paid_order", AsyncMock(side_effect=lambda _s, order, *a, **k: order)),
            patch("app.services.orders._assert_plan_volume_available", AsyncMock()),
        ):
            p.start()
            self.patches.append(p)

    def register_extra_routes(self, app, get_db) -> None:
        register_miniapp_support(app, get_db=get_db)
        register_miniapp_codes(app, get_db=get_db)
        register_miniapp_loyalty(app, get_db=get_db)

    def headers(self, *, key: str | None = KEY, token: str = SHOP_TOKEN) -> dict[str, str]:
        headers = {"X-Telegram-Init-Data": signed(token, CUSTOMER)}
        if key:
            headers["X-Mini-Shop"] = key
        return headers

    async def post(self, path: str, body: dict | None = None, **kwargs):
        return await self.client.post(path, json=body or {}, headers=self.headers(**kwargs))

    async def purses(self) -> tuple[int, int]:
        async with self.Session() as session:
            platform = int((await session.get(BotUser, 10)).wallet_balance)
            shop = await session.scalar(
                select(ShopWallet.balance).where(ShopWallet.user_id == 10, ShopWallet.reseller_id == SHOP)
            )
            return platform, int(shop or 0)


class ShopSupportTests(ShopExtrasFixture):
    async def test_a_ticket_opens_at_the_shops_desk_and_alerts_through_its_bot(self) -> None:
        response = await self.post("/api/mini/support/tickets", {"subject": "help", "body": "<b>hi</b>"})
        self.assertEqual(response.status_code, 200, response.text)
        async with self.Session() as session:
            ticket = await session.get(Ticket, response.json()["ticket_id"])
        self.assertEqual(ticket.reseller_id, SHOP)
        self.assertEqual(self.opener.await_args.kwargs["shop_id"], SHOP)
        self.assertEqual(self.new_ticket.await_args.kwargs["ticket_reseller_id"], SHOP)

    async def test_tickets_of_another_desk_cannot_be_listed_read_or_answered(self) -> None:
        own = (await self.post("/api/mini/support/tickets", {"subject": "help", "body": "hi"})).json()["ticket_id"]
        listed = (await self.client.get("/api/mini/support", headers=self.headers())).json()["tickets"]
        self.assertEqual([t["id"] for t in listed], [own])
        for foreign in (PLATFORM_TICKET, OTHER_SHOP_TICKET):
            with self.subTest(ticket=foreign):
                read = await self.client.get(f"/api/mini/support/tickets/{foreign}", headers=self.headers())
                self.assertEqual(read.status_code, 404)
                reply = await self.post(f"/api/mini/support/tickets/{foreign}/messages", {"body": "x"})
                self.assertEqual(reply.status_code, 400)
        platform = (await self.client.get("/api/mini/support", headers=self.headers(key=None, token=MAIN_TOKEN))).json()
        self.assertEqual([t["id"] for t in platform["tickets"]], [PLATFORM_TICKET])


class ShopCodeTests(ShopExtrasFixture):
    async def checkout(self, code: str, plan_id: int = SHOP_PLAN, **kwargs):
        return await self.post("/api/mini/checkout", {"plan_id": plan_id, "price": 5_000, "code": code}, **kwargs)

    async def test_a_shop_discount_code_discounts_a_shop_order_paid_from_the_shop_purse(self) -> None:
        response = await self.checkout("SHOPOFF")
        self.assertEqual(response.status_code, 200, response.text)
        checkout = response.json()["checkout"]
        self.assertEqual(checkout["amount"], 4_000)
        pay = await self.post(f"/api/mini/checkout/{checkout['order_id']}/pay")
        self.assertEqual(pay.status_code, 200, pay.text)
        self.assertEqual(pay.json()["wallet"], 3_000)
        self.assertEqual(await self.purses(), (50_000, 3_000))
        async with self.Session() as session:
            order = await session.get(Order, checkout["order_id"])
        self.assertEqual(order.reseller_id, SHOP)

    async def test_platform_codes_do_not_discount_a_shop_order(self) -> None:
        for code in ("PLATOFF", "LEGACY50"):
            with self.subTest(code=code):
                self.assertEqual((await self.checkout(code)).status_code, 400)
        async with self.Session() as session:
            statuses = set(await session.scalars(select(Order.status)))
        self.assertEqual(statuses, {OrderStatus.CANCELLED.value}, "a refused code cancels the order it was tried on")
        self.assertEqual(await self.purses(), (50_000, 7_000))

    async def test_a_platform_order_cannot_be_paid_or_cancelled_from_the_shop(self) -> None:
        response = await self.checkout("PLATOFF", plan_id=PLATFORM_PLAN, key=None, token=MAIN_TOKEN)
        self.assertEqual(response.status_code, 200, response.text)
        order_id = response.json()["checkout"]["order_id"]
        for action in ("pay", "cancel"):
            with self.subTest(action=action):
                self.assertEqual((await self.post(f"/api/mini/checkout/{order_id}/{action}")).status_code, 400)
        async with self.Session() as session:
            self.assertEqual((await session.get(Order, order_id)).status, OrderStatus.PENDING.value)

    async def test_gift_codes_credit_the_shop_purse_and_only_the_shops_own_work(self) -> None:
        for code in ("PLATGIFT", "OTHERGIFT"):
            with self.subTest(code=code):
                self.assertEqual((await self.post("/api/mini/wallet/redeem", {"code": code})).status_code, 400)
        response = await self.post("/api/mini/wallet/redeem", {"code": "SHOPGIFT"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["wallet"], 10_000)
        self.assertEqual(await self.purses(), (50_000, 10_000))


class ShopClubTests(ShopExtrasFixture):
    async def test_the_club_shows_the_shops_points_rewards_and_invite_bot(self) -> None:
        club = (await self.client.get("/api/mini/club", headers=self.headers())).json()["club"]
        self.assertEqual(club["points"], 300)
        self.assertEqual([r["id"] for r in club["rewards"]], [81])
        self.assertEqual([h["id"] for h in club["history"]], [91])
        self.assertIn("shop5bot", club["referral"]["link"])
        history = (await self.client.get("/api/mini/club/history", headers=self.headers())).json()["items"]
        self.assertEqual([h["id"] for h in history], [91])

    async def test_redeeming_spends_shop_points_into_the_shop_purse(self) -> None:
        refused = await self.post("/api/mini/club/rewards/82/redeem")
        self.assertEqual(refused.status_code, 400, "the platform's reward is not in the shop's club")
        response = await self.post("/api/mini/club/rewards/81/redeem")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["points"], 200)
        self.assertEqual(await self.purses(), (50_000, 9_000))
        async with self.Session() as session:
            self.assertEqual(int((await session.get(BotUser, 10)).points_balance), 900, "platform points untouched")
            spent = await session.scalar(
                select(PointsTransaction.reseller_id).where(PointsTransaction.amount < 0)
            )
        self.assertEqual(spent, SHOP)


if __name__ == "__main__":
    unittest.main()
