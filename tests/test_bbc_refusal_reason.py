"""«بزن به کارت» refusals: the shop's staff learn the gateway's reason, customers keep the fixed text."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Payment, PaymentMethod, PaymentSettlement, PaymentStatus, Role
from app.services import bbc_settlement as flow
from app.services.payment_providers import bezanbecard as bbc
from app.services.users import clear_settings_cache, set_setting

KEY = "platform-key-123456"
VALIDATION_BODY = {
    "code": "validation_error",
    "detail": [
        {"loc": ["body", "callback_url"], "msg": "host <b>not</b> allowed https://p/cb?t=SECRET1", "input": "https://p/cb?t=SECRET1"},
    ],
}


class RefusalReasonTests(unittest.TestCase):
    def test_reason_names_the_field_and_hides_link_tokens(self) -> None:
        reason = bbc.refusal_reason(VALIDATION_BODY)
        self.assertTrue(reason.startswith("callback_url: host"), reason)
        self.assertNotIn("SECRET1", reason)

    def test_other_shapes(self) -> None:
        self.assertEqual(bbc.refusal_reason({"detail": "amount too small"}), "amount too small")
        self.assertEqual(bbc.refusal_reason({"message": "bad\n\x00 url"}), "bad url")
        self.assertEqual(bbc.refusal_reason({"detail": "x" * 1000}), "x" * bbc.MAX_REASON)
        self.assertEqual(bbc.refusal_reason("not json"), "")
        self.assertEqual(bbc.refusal_reason({}), "")


class RefusalClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await bbc.close_client()

    async def test_refusal_carries_the_reason_but_shows_the_fixed_text(self) -> None:
        bbc._client = httpx.AsyncClient(
            base_url=bbc.API_BASE, transport=httpx.MockTransport(lambda _r: httpx.Response(422, json=VALIDATION_BODY)),
        )
        request = bbc.build_order_request(
            amount_toman=50_000, merchant_order_id="7", callback_url="https://p/cb", return_url="https://p/r",
            description="d", idempotency_key="k",
        )
        with self.assertRaises(bbc.BbcRejected) as caught:
            await bbc.create_order(api_key=KEY, request=request)
        self.assertEqual(str(caught.exception), bbc.REJECTED_TEXT)
        self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "validation_error"))
        self.assertIn("callback_url", caught.exception.reason)


class RefusalNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        flow._last_refusal_notice.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'refusal.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="c1", role=Role.USER.value))
            await session.commit()
            await set_setting(session, "pay_bbc_enabled", "1")
            await set_setting(session, "bbc_api_key", KEY)
        clear_settings_cache()
        self.notify = AsyncMock(return_value=1)
        for patcher in (
            patch("app.services.public_https.https_is_active", return_value=True),
            patch("app.services.public_https.public_panel_base_url", return_value="https://panel.test:9000"),
            patch("app.services.notifications.notify_settlement_review", self.notify),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        flow._last_refusal_notice.clear()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def refused_checkout(self) -> tuple[str, PaymentSettlement]:
        refusal = bbc.BbcRejected(
            bbc.REJECTED_TEXT, status_code=422, code="validation_error", reason="return_url: <host> not allowed",
        )
        async with self.Session() as session:
            payment = Payment(
                user_id=1, amount=50_000, method=PaymentMethod.BBC.value, status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.commit()
            with patch.object(bbc, "create_order", AsyncMock(side_effect=refusal)):
                with self.assertRaises(bbc.BbcRejected) as caught:
                    await flow.start_bbc_checkout(session, payment)
            settlement = await flow._get_by_idem(session, flow.idem_key_bbc(int(payment.id)))
            return str(caught.exception), settlement

    async def test_staff_get_the_escaped_reason_once_per_window(self) -> None:
        customer_text, settlement = await self.refused_checkout()
        self.assertEqual(customer_text, bbc.REJECTED_TEXT, "customers never see the gateway's reason")
        self.assertTrue(str(settlement.error_message).startswith(flow.REJECTED_PREFIX))
        self.notify.assert_awaited_once()
        kwargs = self.notify.await_args.kwargs
        self.assertIn("return_url: &lt;host&gt; not allowed", kwargs["reason"])
        self.assertEqual(kwargs["title"], flow.REFUSAL_TITLE)
        self.assertIsNone(kwargs["shop_id"])
        await self.refused_checkout()
        self.notify.assert_awaited_once()

    async def test_a_failed_notice_does_not_change_the_customer_result(self) -> None:
        self.notify.side_effect = RuntimeError("telegram down")
        customer_text, _settlement = await self.refused_checkout()
        self.assertEqual(customer_text, bbc.REJECTED_TEXT)


if __name__ == "__main__":
    unittest.main()
