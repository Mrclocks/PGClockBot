"""Mini App wallet top-up: options, payment start, receipt upload and gateway check."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendPhoto
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_pages import register_miniapp_pages
from app.api.miniapp_topup import register_miniapp_topup
from app.db.models import Base, BotUser, Payment, WalletTransaction
from app.services import payment_settlement
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from app.services.wallet_topup import RECEIPT_MAX_BYTES, receipt_image_kind

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
CARDS = json.dumps([
    {"id": "c1", "number": "6037991111111111", "holder": "Shop One"},
    {"id": "c2", "number": "6037992222222222", "holder": "Shop Two"},
])
GATEWAYS = json.dumps([
    {"id": "g1", "name": "Gate A", "link": "https://pay.example.com/a?amount={amount}"},
    {"id": "g2", "name": "Gate B", "link": "javascript:alert(1)"},
])
CRYPTO = json.dumps([{"id": "w1", "asset": "USDT", "network": "TRC20", "address": "TXaddress000001"}])
BASE_SETTINGS = {
    "pay_wallet_enabled": "1", "pay_card_enabled": "1", "pay_gateway_enabled": "1",
    "pay_crypto_enabled": "1", "pay_psp_enabled": "0", "pay_bbc_enabled": "0",
    "payment_cards": CARDS, "payment_gateways": GATEWAYS, "payment_crypto_wallets": CRYPTO,
    "wallet_topup_presets": "100000, 250000",
}


class FakeBot:
    """Records photos sent to customers and answers like Telegram."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail: Exception | None = None
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_photo(self, *, chat_id, photo, caption):
        if self.fail:
            raise self.fail
        self.sent.append({"chat_id": chat_id, "caption": caption, "size": len(photo.data)})
        await asyncio.sleep(0)
        n = len(self.sent)
        return SimpleNamespace(photo=[SimpleNamespace(file_id=f"file-{n}", file_unique_id=f"uniq-{n}")])


class MiniAppTopupTestCase(unittest.IsolatedAsyncioTestCase):
    caller_id = 1

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="buyer", role="user", wallet_balance=0),
            BotUser(id=2, telegram_id=102, referral_code="other", role="user", wallet_balance=0),
            BotUser(id=4, telegram_id=104, referral_code="admin", role="admin"),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.settings = dict(BASE_SETTINGS)
        self.bot = FakeBot()
        self.auto_approve = "0"
        payment_settlement._recent_psp_checks.clear()

        async def load_user(*_args):
            return await self.session.scalar(select(BotUser).where(BotUser.id == self.caller_id))

        async def settings(*_args, **_kwargs):
            return dict(self.settings)

        async def get_setting(_session, key, default="", **_kwargs):
            return self.auto_approve if key == "auto_approve_payments" else default

        self.notify_pending = AsyncMock()
        self.patches = [
            patch("app.api.miniapp_topup.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()),
            patch("app.api.miniapp_pages.get_all_settings", AsyncMock(side_effect=settings)),
            patch("app.api.miniapp_topup.open_notify_bot_for_user", AsyncMock(side_effect=lambda *a, **k: (self.bot, False))),
            patch("app.services.receipts.download_receipt_sha256", AsyncMock(return_value=None)),
            patch("app.services.receipts.notify_pending_approval", self.notify_pending),
            patch("app.services.receipts.notify_auto_approve", AsyncMock()),
            patch("app.services.receipts.notify_wallet_topup_ok", AsyncMock()),
            patch("app.services.receipts.send_delivery_to_user", AsyncMock()),
            patch("app.services.receipts.get_setting", AsyncMock(side_effect=get_setting)),
            patch("app.services.miniapp_auth.is_bot_platform_admin", side_effect=lambda u: u.role == "admin"),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_pages(app, render=lambda *args: "", get_db=get_db)
        register_miniapp_topup(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def start(self, **body):
        return await self.client.post("/api/mini/topup", json={"amount": 100000, "method": "card", "dest_id": "c1", **body})

    async def upload(self, payment_id: int, data: bytes = JPEG):
        return await self.client.post(f"/api/mini/topup/{payment_id}/receipt", content=data,
                                      headers={"Content-Type": "image/jpeg"})

    async def payments(self) -> list[Payment]:
        self.session.expire_all()
        return list(await self.session.scalars(select(Payment).order_by(Payment.id)))

    async def wallet(self, user_id: int = 1) -> int:
        self.session.expire_all()
        return int((await self.session.get(BotUser, user_id)).wallet_balance or 0)


class TopupOptionsTests(MiniAppTopupTestCase):
    async def test_lists_enabled_methods_with_destination_choices(self) -> None:
        self.settings["pay_bbc_enabled"] = "1"
        body = (await self.client.get("/api/mini/topup")).json()
        methods = {m["key"]: m for m in body["methods"]}
        self.assertEqual(list(methods), ["card", "gateway", "crypto"])
        self.assertEqual([c["id"] for c in methods["card"]["choices"]], ["c1", "c2"])
        self.assertNotIn("6037991111111111", json.dumps(body), "full card numbers appear only after starting")
        self.assertEqual(methods["crypto"]["choices"], [], "a single destination needs no choice")
        self.assertTrue(methods["card"]["needs_receipt"])
        self.assertEqual(body["presets"], [100000, 250000])
        self.assertTrue(body["bot_only"])

    async def test_admin_labels_show_as_plain_text(self) -> None:
        self.settings["btn_pay_card"] = "💳 <b>کارت</b> &amp; بانک"
        body = (await self.client.get("/api/mini/topup")).json()
        self.assertEqual(body["methods"][0]["label"], "💳 کارت & بانک")

    async def test_random_card_mode_hides_the_card_choice(self) -> None:
        self.settings["payment_cards_random"] = "1"
        body = (await self.client.get("/api/mini/topup")).json()
        self.assertEqual(next(m for m in body["methods"] if m["key"] == "card")["choices"], [])

    async def test_admin_cannot_top_up(self) -> None:
        self.caller_id = 4
        self.assertEqual((await self.client.get("/api/mini/topup")).status_code, 403)
        self.assertEqual((await self.start()).status_code, 403)
        self.assertEqual(await self.payments(), [])


class TopupStartTests(MiniAppTopupTestCase):
    async def test_card_top_up_creates_one_pending_platform_payment(self) -> None:
        response = await self.start(dest_id="c2")
        self.assertEqual(response.status_code, 200, response.text)
        topup = response.json()["topup"]
        self.assertEqual(topup["card_number"], "6037992222222222")
        self.assertTrue(topup["needs_receipt"])
        [payment] = await self.payments()
        self.assertEqual((payment.user_id, payment.amount, payment.method, payment.status), (1, 100000, "card", "pending"))
        self.assertTrue(payment.is_wallet_topup)
        self.assertIsNone(payment.wallet_shop_id)
        self.assertEqual(await self.wallet(), 0, "starting a top-up never credits the wallet")

    async def test_rejects_bad_amounts_methods_and_destinations(self) -> None:
        cases = [
            {"amount": True}, {"amount": 999}, {"amount": 500_000_001}, {"amount": 1000.5},
            {"amount": "100000"}, {"amount": None}, {"method": "bbc"}, {"method": "wallet"},
            {"method": ["card"]}, {"dest_id": "c9"}, {"dest_id": "../../etc"}, {"dest_id": 5},
            {"dest_id": None},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                self.assertEqual((await self.start(**extra)).status_code, 400)
        self.settings["pay_crypto_enabled"] = "0"
        self.assertEqual((await self.start(method="crypto", dest_id=None)).status_code, 400)
        self.assertEqual(await self.payments(), [])

    async def test_gateway_links_are_rendered_and_unsafe_links_dropped(self) -> None:
        good = (await self.start(method="gateway", dest_id="g1")).json()["topup"]
        self.assertEqual(good["gateway_link"], "https://pay.example.com/a?amount=100000")
        bad = (await self.start(method="gateway", dest_id="g2")).json()["topup"]
        self.assertNotIn("gateway_link", bad)

    async def test_open_top_ups_without_receipt_are_limited(self) -> None:
        for _ in range(3):
            self.assertEqual((await self.start()).status_code, 200)
        self.assertEqual((await self.start()).status_code, 400)
        self.caller_id = 2
        self.assertEqual((await self.start()).status_code, 200, "the limit is per customer")

    async def test_online_gateway_returns_only_an_http_checkout(self) -> None:
        self.settings["pay_psp_enabled"] = "1"
        settlement = SimpleNamespace(id=77)
        with patch("app.services.wallet_topup.create_psp_checkout", AsyncMock(return_value=settlement)), \
                patch("app.services.wallet_topup.public_checkout_button_url", return_value="https://psp.example/pay/1"):
            topup = (await self.start(method="psp", dest_id=None)).json()["topup"]
        self.assertEqual((topup["checkout_url"], topup["settlement_id"]), ("https://psp.example/pay/1", 77))
        self.assertFalse(topup["needs_receipt"])

    async def test_failed_checkout_closes_the_payment_it_created(self) -> None:
        self.settings["pay_psp_enabled"] = "1"
        failing = AsyncMock(side_effect=ValueError("درگاه در دسترس نیست"))
        with patch("app.services.wallet_topup.create_psp_checkout", failing):
            for _ in range(4):
                self.assertEqual((await self.start(method="psp", dest_id=None)).status_code, 400)
        payments = await self.payments()
        self.assertEqual({p.status for p in payments}, {"rejected"})
        self.assertEqual(len(payments), 4, "closed attempts do not count toward the open top-up limit")
        self.assertEqual(await self.wallet(), 0)


class TopupReceiptTests(MiniAppTopupTestCase):
    async def started(self, **body) -> int:
        return (await self.start(**body)).json()["topup"]["payment_id"]

    async def test_receipt_goes_to_the_customers_chat_and_to_review(self) -> None:
        payment_id = await self.started()
        response = await self.upload(payment_id, PNG)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual([s["chat_id"] for s in self.bot.sent], [101])
        [payment] = await self.payments()
        self.assertEqual(payment.receipt_file_id, "file-1")
        self.notify_pending.assert_awaited_once()
        self.assertEqual(await self.wallet(), 0)

    async def test_second_receipt_is_refused_before_sending_anything(self) -> None:
        payment_id = await self.started()
        self.assertEqual((await self.upload(payment_id)).status_code, 200)
        self.assertEqual((await self.upload(payment_id)).status_code, 400)
        self.assertEqual(len(self.bot.sent), 1)
        self.notify_pending.assert_awaited_once()

    async def test_concurrent_receipts_attach_once(self) -> None:
        payment_id = await self.started()
        first, second = await asyncio.gather(self.upload(payment_id), self.upload(payment_id))
        self.assertEqual(sorted([first.status_code, second.status_code]), [200, 400])
        self.notify_pending.assert_awaited_once()

    async def test_auto_approved_receipt_credits_the_wallet_once(self) -> None:
        self.auto_approve = "1"
        payment_id = await self.started()
        response = await self.upload(payment_id)
        self.assertEqual(response.json()["status"], "approved", response.text)
        self.assertEqual(await self.wallet(), 100000)
        self.assertEqual((await self.upload(payment_id)).status_code, 400)
        self.assertEqual(await self.wallet(), 100000)
        credits = await self.session.scalar(select(func.count()).select_from(WalletTransaction).where(WalletTransaction.user_id == 1))
        self.assertEqual(credits, 1)

    async def test_another_customers_payment_is_not_found(self) -> None:
        payment_id = await self.started()
        self.caller_id = 2
        response = await self.upload(payment_id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("پیدا نشد", response.json()["detail"])
        self.assertEqual(self.bot.sent, [])
        self.assertIsNone((await self.payments())[0].receipt_file_id)

    async def test_shop_wallet_and_order_payments_are_not_reachable(self) -> None:
        self.session.add_all([
            Payment(id=50, user_id=1, amount=5000, method="card", status="pending", is_wallet_topup=True, wallet_shop_id=3),
            Payment(id=51, user_id=1, amount=5000, method="card", status="pending", is_wallet_topup=False),
            Payment(id=52, user_id=1, amount=5000, method="psp", status="pending", is_wallet_topup=True),
            Payment(id=53, user_id=1, amount=5000, method="card", status="approved", is_wallet_topup=True),
        ])
        await self.session.commit()
        for payment_id in (50, 51, 52, 53, 999999, 0):
            with self.subTest(payment_id=payment_id):
                self.assertEqual((await self.upload(payment_id)).status_code, 400)
        self.assertEqual(self.bot.sent, [])

    async def test_only_real_jpeg_or_png_under_the_size_limit(self) -> None:
        payment_id = await self.started()
        for data in (b"", b"GIF89a....", b"<svg onload=alert(1)>", b"%PDF-1.7"):
            with self.subTest(data=data[:8]):
                self.assertEqual((await self.upload(payment_id, data)).status_code, 400)
        too_big = await self.upload(payment_id, b"\xff\xd8\xff" + b"\x00" * RECEIPT_MAX_BYTES)
        self.assertEqual(too_big.status_code, 413)

        async def chunked():
            # No Content-Length: the stream itself must be cut off.
            yield b"\xff\xd8\xff"
            for _ in range(RECEIPT_MAX_BYTES // (1024 * 1024) + 1):
                yield b"\x00" * (1024 * 1024)

        streamed = await self.client.post(f"/api/mini/topup/{payment_id}/receipt", content=chunked())
        self.assertEqual(streamed.status_code, 413)
        self.assertEqual(self.bot.sent, [])

    async def test_telegram_failure_leaves_the_payment_open(self) -> None:
        payment_id = await self.started()
        self.bot.fail = TelegramForbiddenError(method=SendPhoto(chat_id=101, photo="x"), message="blocked")
        response = await self.upload(payment_id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Start", response.json()["detail"])
        [payment] = await self.payments()
        self.assertEqual((payment.status, payment.receipt_file_id), ("pending", None))
        self.bot.fail = None
        self.assertEqual((await self.upload(payment_id)).status_code, 200, "the customer can try again")

    async def test_recent_top_ups_show_only_own_platform_payments(self) -> None:
        payment_id = await self.started()
        self.session.add(Payment(user_id=1, amount=7000, method="card", status="pending", is_wallet_topup=True, wallet_shop_id=3))
        self.session.add(Payment(user_id=2, amount=8000, method="card", status="pending", is_wallet_topup=True))
        await self.session.commit()
        recent = (await self.client.get("/api/mini/topup")).json()["recent"]
        self.assertEqual([(r["id"], r["waiting_receipt"]) for r in recent], [(payment_id, True)])

    def test_image_signatures(self) -> None:
        self.assertEqual(receipt_image_kind(JPEG), "jpg")
        self.assertEqual(receipt_image_kind(PNG), "png")
        self.assertIsNone(receipt_image_kind(b"\xff\xd8"))


class TopupGatewayCheckTests(MiniAppTopupTestCase):
    async def test_check_is_scoped_and_throttled(self) -> None:
        with patch("app.api.miniapp_topup.check_psp_payment", AsyncMock(side_effect=ValueError("پرداخت پیدا نشد"))) as check:
            first = await self.client.post("/api/mini/topup/check", json={"settlement_id": 9})
            again = await self.client.post("/api/mini/topup/check", json={"settlement_id": 9})
        self.assertEqual(first.status_code, 400)
        self.assertEqual(again.status_code, 429)
        check.assert_awaited_once()
        self.assertEqual(check.await_args.kwargs, {"settlement_id": 9, "user_id": 1, "shop_id": None})
        for bad in ({"settlement_id": True}, {"settlement_id": "9"}, {"settlement_id": -1}, []):
            with self.subTest(bad=bad):
                self.assertEqual((await self.client.post("/api/mini/topup/check", json=bad)).status_code, 400)


if __name__ == "__main__":
    unittest.main()
