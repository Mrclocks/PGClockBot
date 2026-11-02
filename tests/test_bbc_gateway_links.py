"""«بزن به کارت» links: both on the panel host without its port."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Payment, PaymentMethod, PaymentStatus, Role
from app.services import bbc_settlement as flow
from app.services.payment_providers import bezanbecard as bbc
from app.services.users import clear_settings_cache, set_setting

KEY = "platform-key-123456"
PANEL = "https://panel.test:9000"


def _order(payment_id: int) -> bbc.BbcOrder:
    return bbc.BbcOrder(
        order_id="ord_1",
        payment_token="tok_abcdefgh",
        status="pending",
        unique_amount_rial=500_043,
        base_amount_rial=500_000,
        merchant_order_id=str(payment_id),
        payment_url="https://bezanbecard.ir/pay/tok_abcdefgh",
    )


class GatewayOriginTests(unittest.TestCase):
    def test_panel_port_is_dropped(self) -> None:
        self.assertEqual(flow.gateway_origin("https://panel.test:9000"), "https://panel.test")
        self.assertEqual(flow.gateway_origin("https://panel.test:443"), "https://panel.test")
        self.assertEqual(flow.gateway_origin("https://panel.test"), "https://panel.test")

    def test_no_host_gives_no_origin(self) -> None:
        self.assertEqual(flow.gateway_origin(""), "")


class GatewayLinkRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'links.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="c1", role=Role.USER.value))
            await session.commit()
            await set_setting(session, "pay_bbc_enabled", "1")
            await set_setting(session, "bbc_api_key", KEY)
        clear_settings_cache()
        for patcher in (
            patch("app.services.public_https.https_is_active", return_value=True),
            patch("app.services.public_https.public_panel_base_url", return_value=PANEL),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _sent_request(self) -> bbc.BbcOrderRequest:
        async with self.Session() as session:
            payment = Payment(
                user_id=1,
                amount=50_000,
                method=PaymentMethod.BBC.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.commit()
            create = AsyncMock(return_value=_order(int(payment.id)))
            with patch.object(bbc, "create_order", create):
                await flow.start_bbc_checkout(session, payment)
            return create.await_args.kwargs["request"]

    async def test_callback_has_no_panel_port(self) -> None:
        request = await self._sent_request()
        self.assertTrue(
            request.callback_url.startswith("https://panel.test/payments/settlement/bbc/"),
            request.callback_url,
        )
        self.assertNotIn(":9000", request.callback_url)

    async def test_payer_returns_to_the_panel_page_without_port(self) -> None:
        request = await self._sent_request()
        self.assertTrue(
            request.return_url.startswith("https://panel.test/payments/settlement/bbc/"),
            request.return_url,
        )
        self.assertIn("/return/", request.return_url)


if __name__ == "__main__":
    unittest.main()
