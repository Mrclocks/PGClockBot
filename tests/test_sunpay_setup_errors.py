"""A shop's SunPay setup mistakes reach staff, not the paying customer."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Payment, PaymentMethod, PaymentSettlement, PaymentStatus, Role, SettlementStatus
from app.services import public_https as pub
from app.services.payment_providers import sunpay as sunpay_mod
from app.services.payment_providers.base import CheckoutRequest
from app.services.payment_providers.sunpay import (
    CUSTOMER_UNAVAILABLE,
    SunPayAdapter,
    SunPayGatewayError,
    close_client,
)
from app.services.payment_settlement import create_psp_checkout
from app.services.redact import user_safe_error
from app.services.users import clear_settings_cache, set_setting

PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"


def _request() -> CheckoutRequest:
    return CheckoutRequest(
        settlement_id=1,
        payment_id=1,
        amount=50_000,
        currency="IRT",
        description="t",
        callback_url="https://bot.test/payments/settlement/psp/sunpay/return/1",
        return_url="https://bot.test/payments/settlement/psp/sunpay/return/1",
        metadata={},
    )


class SunPaySetupErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _create_error(self, adapter: SunPayAdapter, *, dns: str = "8.8.8.8") -> SunPayGatewayError:
        with patch.object(sunpay_mod, "_resolve_host_addresses", return_value=[dns]):
            with self.assertRaises(SunPayGatewayError) as ctx:
                await adapter.create_checkout(_request())
        return ctx.exception

    async def test_missing_api_key_shows_customer_a_neutral_message(self) -> None:
        exc = await self._create_error(SunPayAdapter(api_key="", pay_url=PAY_URL))
        self.assertEqual(user_safe_error(exc), CUSTOMER_UNAVAILABLE)
        self.assertIn("کلید API", exc.admin_detail)

    async def test_bad_pay_url_shows_customer_a_neutral_message(self) -> None:
        for pay_url in ("", "http://pay.example.com", "https://127.0.0.1"):
            with self.subTest(pay_url=pay_url):
                exc = await self._create_error(SunPayAdapter(api_key=API_KEY, pay_url=pay_url))
                self.assertEqual(user_safe_error(exc), CUSTOMER_UNAVAILABLE)
                self.assertIn("سان‌پی", exc.admin_detail)

    async def test_private_dns_answer_shows_customer_a_neutral_message(self) -> None:
        exc = await self._create_error(SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL), dns="10.0.0.5")
        self.assertEqual(user_safe_error(exc), CUSTOMER_UNAVAILABLE)


class SunPaySetupErrorSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.config import get_settings

        get_settings.cache_clear()
        clear_settings_cache()
        await close_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    async def test_checkout_keeps_setup_detail_for_staff_only(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "pay_psp_enabled", "1")
            await set_setting(session, "psp_provider", "sunpay")
            await set_setting(session, "psp_pay_url", PAY_URL)
            user = BotUser(telegram_id=7001, username="u", full_name="u", referral_code="r8", role=Role.USER.value)
            session.add(user)
            await session.flush()
            payment = Payment(
                user_id=user.id,
                amount=50_000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.commit()
            with patch.object(pub, "https_public_base", return_value="https://bot.test"):
                with self.assertRaises(SunPayGatewayError) as ctx:
                    await create_psp_checkout(session, payment)
            self.assertEqual(user_safe_error(ctx.exception), CUSTOMER_UNAVAILABLE)
            settlement = (await session.execute(PaymentSettlement.__table__.select())).one()
            self.assertEqual(settlement.status, SettlementStatus.FAILED.value)
            self.assertIn("کلید API", settlement.error_message)


if __name__ == "__main__":
    unittest.main()
