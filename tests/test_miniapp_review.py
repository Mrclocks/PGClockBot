"""Mini App receipt review: who reviews what, approving and rejecting once, receipt photos."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_review import register_miniapp_review
from app.db.models import Base, BotUser, Order, Payment, ResellerProfile, ShopWallet, WalletTransaction
from app.services.receipt_review import may_review_receipts
from app.services.secret_box import seal_bot_token
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

ADMIN, OWNER, OWNER_NO_PERM, OTHER_OWNER, CUSTOMER = 1, 3, 4, 7, 5


class MiniAppReviewTestCase(unittest.IsolatedAsyncioTestCase):
    caller_id = ADMIN

    async def asyncSetUp(self) -> None:
        # A file database so concurrent requests each get their own session and connection.
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'review.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all([
            BotUser(id=ADMIN, telegram_id=901, referral_code="adm", role="admin"),
            BotUser(id=OWNER, telegram_id=903, referral_code="own", role="reseller"),
            BotUser(id=OWNER_NO_PERM, telegram_id=904, referral_code="own2", role="reseller"),
            BotUser(id=OTHER_OWNER, telegram_id=907, referral_code="own3", role="reseller"),
            BotUser(id=CUSTOMER, telegram_id=905, referral_code="cus", role="user", full_name="<b>Ali</b>", wallet_balance=0),
            ResellerProfile(user_id=OWNER, web_permissions='["payments"]', bot_token=seal_bot_token("SHOP3_TOKEN")),
            ResellerProfile(user_id=OWNER_NO_PERM, web_permissions='["orders"]'),
            ResellerProfile(user_id=OTHER_OWNER, web_permissions='["payments"]'),
            Order(id=20, user_id=CUSTOMER, amount=50_000, status="awaiting_approval", reseller_id=OWNER),
            Order(id=21, user_id=CUSTOMER, amount=50_000, status="awaiting_approval", reseller_id=OTHER_OWNER),
            Payment(id=10, user_id=CUSTOMER, amount=70_000, status="pending", receipt_file_id="PLATFORM_FILE", is_wallet_topup=True),
            Payment(id=11, user_id=CUSTOMER, order_id=20, amount=50_000, status="pending", receipt_file_id="SHOP_FILE"),
            Payment(id=12, user_id=CUSTOMER, amount=30_000, status="pending", receipt_file_id="SHOP_TOPUP", is_wallet_topup=True, wallet_shop_id=OWNER),
            Payment(id=13, user_id=CUSTOMER, order_id=21, amount=50_000, status="pending", receipt_file_id="OTHER_FILE"),
            Payment(id=14, user_id=CUSTOMER, amount=1_000, status="pending", is_wallet_topup=True),
            Payment(id=15, user_id=CUSTOMER, amount=1_000, status="pending", receipt_file_id="stars:charge", is_wallet_topup=True),
            Payment(id=16, user_id=CUSTOMER, amount=9_000, status="pending", receipt_file_id="OTHER_TOPUP", is_wallet_topup=True, wallet_shop_id=OTHER_OWNER),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        self.open_bot = AsyncMock(return_value=(self.bot, True))
        self.notify_payer = AsyncMock(return_value=True)
        self.fetch = AsyncMock(return_value=(b"\xff\xd8jpeg", "image/jpeg"))

        async def load_user(session, _request):
            return await session.scalar(select(BotUser).where(BotUser.id == self.caller_id))

        self.patches = [
            patch("app.api.miniapp_review.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.services.miniapp_auth.is_bot_platform_admin", side_effect=lambda u: u.role == "admin"),
            patch("app.services.receipt_review.open_notify_bot_for_user", self.open_bot),
            patch("app.services.receipt_review.notify_payer", self.notify_payer),
            patch("app.services.receipt_review.fetch_telegram_file", self.fetch),
            patch("app.services.receipt_files.get_settings", return_value=SimpleNamespace(bot_token="PLATFORM_TOKEN")),
            # Telegram is not under test: delivery and review-card edits are recorded only.
            patch("app.services.payment_review_actions.send_delivery_to_user", AsyncMock(return_value="ok")),
            patch("app.services.notifications.notify_wallet_topup_ok", AsyncMock()),
            patch("app.services.orders._sync_payment_review_messages", AsyncMock()),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            async with self.Session() as session:
                yield session

        register_miniapp_review(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def queue_ids(self) -> list[int]:
        response = await self.client.get("/api/mini/review")
        self.assertEqual(response.status_code, 200, response.text)
        return [item["id"] for item in response.json()["items"]]

    async def act(self, payment_id: int, action: str):
        return await self.client.post(f"/api/mini/review/{payment_id}/{action}")

    async def payment(self, payment_id: int) -> Payment:
        self.session.expire_all()
        return await self.session.get(Payment, payment_id)

    async def wallet(self) -> int:
        self.session.expire_all()
        return int((await self.session.get(BotUser, CUSTOMER)).wallet_balance or 0)


class ReviewScopeTests(MiniAppReviewTestCase):
    async def test_admin_sees_platform_receipts_only(self) -> None:
        self.assertEqual(await self.queue_ids(), [10])
        item = (await self.client.get("/api/mini/review")).json()["items"][0]
        self.assertEqual((item["kind"], item["amount"], item["payer"]), ("topup", 70_000, "<b>Ali</b>"))
        self.assertNotIn("PLATFORM_FILE", (await self.client.get("/api/mini/review")).text)

    async def test_shop_owner_sees_their_shop_only(self) -> None:
        self.caller_id = OWNER
        self.assertEqual(await self.queue_ids(), [11, 12])
        self.caller_id = OTHER_OWNER
        self.assertEqual(await self.queue_ids(), [13, 16])

    async def test_no_permission_and_customers_are_denied(self) -> None:
        for caller in (OWNER_NO_PERM, CUSTOMER):
            with self.subTest(caller=caller):
                self.caller_id = caller
                self.assertEqual((await self.client.get("/api/mini/review")).status_code, 403)
                self.assertEqual((await self.act(10, "approve")).status_code, 403)
        self.assertEqual((await self.payment(10)).status, "pending")

    async def test_admin_by_id_only_is_denied_like_in_the_bot(self) -> None:
        # The bot reviews with role "admin" only; an ADMIN_IDS account with another role gets nothing.
        with patch("app.services.miniapp_auth.is_bot_platform_admin", return_value=True):
            self.caller_id = CUSTOMER
            self.assertEqual((await self.client.get("/api/mini/review")).status_code, 403)

    async def test_review_flag_matches_who_may_review(self) -> None:
        expected = {ADMIN: True, OWNER: True, OTHER_OWNER: True, OWNER_NO_PERM: False, CUSTOMER: False}
        for user_id, allowed in expected.items():
            with self.subTest(user_id=user_id):
                self.assertEqual(await may_review_receipts(self.session, await self.session.get(BotUser, user_id)), allowed)

    async def test_out_of_scope_payments_cannot_be_touched(self) -> None:
        cases = {ADMIN: (11, 12, 13), OWNER: (10, 13), OTHER_OWNER: (10, 11, 12)}
        for caller, payment_ids in cases.items():
            self.caller_id = caller
            for payment_id in payment_ids:
                for action in ("approve", "reject"):
                    with self.subTest(caller=caller, payment=payment_id, action=action):
                        self.assertEqual((await self.act(payment_id, action)).status_code, 404)
                self.assertEqual((await self.client.get(f"/api/mini/review/{payment_id}/receipt")).status_code, 404)
        for payment_id in (10, 11, 12, 13):
            self.assertEqual((await self.payment(payment_id)).status, "pending")
        self.notify_payer.assert_not_awaited()
        self.fetch.assert_not_awaited()


class ReviewActionTests(MiniAppReviewTestCase):
    async def test_admin_approves_a_platform_topup_once(self) -> None:
        response = await self.act(10, "approve")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.wallet(), 70_000)
        self.assertEqual((await self.payment(10)).status, "approved")
        self.assertEqual((await self.act(10, "approve")).status_code, 400)
        self.assertEqual((await self.act(10, "reject")).status_code, 400)
        self.assertEqual(await self.wallet(), 70_000)
        self.assertEqual(self.open_bot.await_args.kwargs["shop_id"], None)
        self.bot.session.close.assert_awaited()

    async def test_concurrent_approvals_credit_once(self) -> None:
        results = await asyncio.gather(*(self.act(10, "approve") for _ in range(3)))
        self.assertTrue(any(r.status_code == 200 for r in results), [r.text for r in results])
        self.assertEqual(await self.wallet(), 70_000)
        credits = await self.session.scalar(select(func.count()).select_from(WalletTransaction).where(WalletTransaction.user_id == CUSTOMER))
        self.assertEqual(credits, 1)

    async def test_shop_owner_approves_a_shop_topup_into_the_shop_purse(self) -> None:
        self.caller_id = OWNER
        response = await self.act(12, "approve")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(await self.wallet(), 0, "the platform wallet is untouched")
        shop_balance = await self.session.scalar(
            select(ShopWallet.balance).where(ShopWallet.user_id == CUSTOMER, ShopWallet.reseller_id == OWNER)
        )
        self.assertEqual(shop_balance, 30_000)
        self.assertEqual(self.open_bot.await_args.kwargs["shop_id"], OWNER)

    async def test_reject_tells_the_payer_once_through_the_payments_shop(self) -> None:
        self.caller_id = OWNER
        response = await self.act(11, "reject")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.payment(11)).status, "rejected")
        self.session.expire_all()
        self.assertEqual((await self.session.get(Order, 20)).status, "rejected")
        self.assertEqual((await self.act(11, "reject")).status_code, 400)
        self.notify_payer.assert_awaited_once()
        self.assertEqual(self.notify_payer.await_args.kwargs["shop_id"], OWNER)

    async def test_receipt_photo_is_read_with_the_receiving_bots_token(self) -> None:
        response = await self.client.get("/api/mini/review/10/receipt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\xff\xd8jpeg")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(self.fetch.await_args.args, ("PLATFORM_TOKEN", "PLATFORM_FILE"))
        self.caller_id = OWNER
        self.assertEqual((await self.client.get("/api/mini/review/11/receipt")).status_code, 200)
        self.assertEqual(self.fetch.await_args.args, ("SHOP3_TOKEN", "SHOP_FILE"))
        self.assertEqual((await self.client.get("/api/mini/review/12/receipt")).status_code, 200)
        self.assertEqual(self.fetch.await_args.args, ("SHOP3_TOKEN", "SHOP_TOPUP"), "a shop top-up has no order")

    async def test_failed_download_is_a_safe_error(self) -> None:
        self.fetch.side_effect = httpx.ConnectError("api.telegram.org/botSECRET")
        response = await self.client.get("/api/mini/review/10/receipt")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("SECRET", response.text)

    async def test_out_of_range_ids_are_not_found(self) -> None:
        for payment_id in (0, 2**31):
            with self.subTest(payment_id=payment_id):
                self.assertEqual((await self.act(payment_id, "approve")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
