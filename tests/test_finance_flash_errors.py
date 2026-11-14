"""Finance routes flash a redacted, operator-safe error, never raw exception text."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Payment, PaymentStatus
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

LEAKY_ERROR = 'boom File "/app/services/orders.py", line 1 token=SECRET123abc'
SAFE_DOMAIN_ERROR = "این پرداخت قابل تأیید نیست"


class FinanceFlashErrorTests(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add(BotUser(id=9, telegram_id=109, referral_code="cust9", role="user"))
            session.add(Payment(
                id=51, user_id=9, amount=10_000, is_wallet_topup=True, status=PaymentStatus.PENDING.value,
            ))
            await session.commit()

    async def _flash(self, action: str, failure: Exception) -> str:
        target = "approve_payment" if action == "approve" else "reject_payment"
        with patch(f"app.api.app.{target}", AsyncMock(side_effect=failure)):
            response = await self.call(OWNER_SESSION, "POST", f"/payments/51/{action}")
        return parse_qs(urlsplit(response.headers["location"]).query).get("err", [""])[0]

    async def test_internal_error_text_is_not_flashed_to_the_operator(self):
        for action in ("approve", "reject"):
            flash = await self._flash(action, RuntimeError(LEAKY_ERROR))
            self.assertNotIn("SECRET123abc", flash, action)
            self.assertNotIn("/app/services", flash, action)
            self.assertTrue(flash, action)

    async def test_a_domain_error_message_still_reaches_the_operator(self):
        for action in ("approve", "reject"):
            self.assertEqual(await self._flash(action, ValueError(SAFE_DOMAIN_ERROR)), SAFE_DOMAIN_ERROR)
