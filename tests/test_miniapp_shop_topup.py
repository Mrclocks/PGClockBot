"""A shop's Mini App tops up that shop's purse, with its own cards, through its own bot and reviewers."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.api.miniapp_topup import register_miniapp_topup
from app.db.models import Payment, PaymentMethod, PaymentStatus
from app.services.reseller_bots import ShopBotUnavailable
from app.services.users import clear_settings_cache, set_setting
from tests.test_miniapp_shop_readonly import CUSTOMER, KEY, OTHER_KEY, OTHER_SHOP, SHOP, ShopFixture
from tests.test_miniapp_shop_scope import MAIN_TOKEN, OTHER_SHOP_TOKEN, SHOP_TOKEN, signed
from tests.test_miniapp_topup import JPEG, FakeBot

SHOP_CARD = "6037990000005555"
PLATFORM_CARD = "6037990000001111"


def cards(number: str) -> str:
    return json.dumps([{"id": "c1", "number": number, "holder": "holder"}])


class ShopTopupTests(ShopFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            for shop, number in ((None, PLATFORM_CARD), (SHOP, SHOP_CARD)):
                await set_setting(session, "pay_card_enabled", "1", reseller_id=shop)
                await set_setting(session, "payment_cards", cards(number), reseller_id=shop)
        clear_settings_cache()
        self.bot = FakeBot()
        self.opener = AsyncMock(return_value=(self.bot, False))
        self.notify_pending = AsyncMock()
        for p in (
            patch("app.api.miniapp_topup.open_notify_bot_for_user", self.opener),
            patch("app.services.receipts.download_receipt_sha256", AsyncMock(return_value=None)),
            patch("app.services.receipts.notify_pending_approval", self.notify_pending),
        ):
            p.start()
            self.patches.append(p)

    def register_extra_routes(self, app, get_db) -> None:
        register_miniapp_topup(app, get_db=get_db)

    def headers(self, *, key: str | None = KEY, token: str = SHOP_TOKEN) -> dict[str, str]:
        headers = {"X-Telegram-Init-Data": signed(token, CUSTOMER)}
        if key:
            headers["X-Mini-Shop"] = key
        return headers

    async def start(self, **kwargs) -> dict:
        response = await self.client.post(
            "/api/mini/topup", json={"amount": 100_000, "method": "card"}, headers=self.headers(**kwargs)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["topup"]

    async def upload(self, payment_id: int, **kwargs):
        return await self.client.post(
            f"/api/mini/topup/{payment_id}/receipt", content=JPEG,
            headers={**self.headers(**kwargs), "Content-Type": "image/jpeg"},
        )

    async def payment(self, payment_id: int) -> Payment:
        async with self.Session() as session:
            return await session.get(Payment, payment_id)

    async def test_a_shop_top_up_uses_the_shops_card_and_purse(self) -> None:
        topup = await self.start()
        self.assertEqual(topup["card_number"], SHOP_CARD)
        payment = await self.payment(topup["payment_id"])
        self.assertEqual((payment.wallet_shop_id, payment.status), (SHOP, PaymentStatus.PENDING.value))
        platform = await self.start(key=None, token=MAIN_TOKEN)
        self.assertEqual(platform["card_number"], PLATFORM_CARD)
        self.assertIsNone((await self.payment(platform["payment_id"])).wallet_shop_id)

    async def test_a_shop_without_its_own_cards_offers_no_top_up(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "payment_cards", "[]", reseller_id=SHOP)
        clear_settings_cache()
        options = (await self.client.get("/api/mini/topup", headers=self.headers())).json()
        self.assertEqual(options["methods"], [], "the platform's cards are never offered in a shop")
        response = await self.client.post(
            "/api/mini/topup", json={"amount": 100_000, "method": "card"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 400)
        async with self.Session() as session:
            self.assertIsNone(await session.scalar(select(Payment.id)))

    async def test_the_receipt_goes_through_the_shop_bot_to_its_reviewers(self) -> None:
        payment_id = (await self.start())["payment_id"]
        response = await self.upload(payment_id)
        self.assertEqual(response.json()["status"], "pending", response.text)
        self.assertEqual(self.opener.await_args.kwargs["shop_id"], SHOP)
        self.assertEqual(self.bot.sent[0]["chat_id"], CUSTOMER)
        self.assertEqual(int(self.notify_pending.await_args.args[2].wallet_shop_id), SHOP)
        self.assertEqual((await self.payment(payment_id)).receipt_file_id, "file-1")

    async def test_a_receipt_only_reaches_a_top_up_of_the_same_purse(self) -> None:
        shop_payment = (await self.start())["payment_id"]
        platform_payment = (await self.start(key=None, token=MAIN_TOKEN))["payment_id"]
        for payment_id, kwargs in ((platform_payment, {}), (shop_payment, {"key": None, "token": MAIN_TOKEN})):
            with self.subTest(payment_id=payment_id):
                response = await self.upload(payment_id, **kwargs)
                self.assertEqual((response.status_code, response.json()["detail"]), (400, "پرداخت پیدا نشد"))
        self.assertEqual(self.bot.sent, [], "nothing is sent for a payment of another purse")

    async def test_recent_top_ups_are_the_shops_only(self) -> None:
        shop_payment = (await self.start())["payment_id"]
        await self.start(key=None, token=MAIN_TOKEN)
        async with self.Session() as session:
            session.add(Payment(user_id=10, amount=5_000, method=PaymentMethod.CARD.value,
                                status=PaymentStatus.PENDING.value, is_wallet_topup=True, wallet_shop_id=OTHER_SHOP))
            await session.commit()
        recent = (await self.client.get("/api/mini/topup", headers=self.headers())).json()["recent"]
        self.assertEqual([row["id"] for row in recent], [shop_payment])

    async def test_the_gateway_check_is_scoped_to_the_shop(self) -> None:
        check = AsyncMock(side_effect=ValueError("not yours"))
        with patch("app.api.miniapp_topup.check_psp_payment", check):
            response = await self.client.post(
                "/api/mini/topup/check", json={"settlement_id": 7}, headers=self.headers()
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(check.await_args.kwargs["shop_id"], SHOP)

    async def test_a_shop_without_a_reachable_bot_refuses_the_receipt_safely(self) -> None:
        payment_id = (await self.start())["payment_id"]
        self.opener.side_effect = ShopBotUnavailable("no bot")
        response = await self.upload(payment_id)
        self.assertEqual(response.status_code, 503)
        self.assertIsNone((await self.payment(payment_id)).receipt_file_id)

    async def test_another_shops_customer_cannot_top_up_here(self) -> None:
        response = await self.client.get("/api/mini/topup", headers=self.headers(key=OTHER_KEY, token=OTHER_SHOP_TOKEN))
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
