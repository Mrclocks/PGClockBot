"""The finance payments tab still lists every payment when a receipt-match suggestion fails.

The suggestion used to roll the whole session back on a failure. That expired the payments loaded
for the page, so the next row and the template read expired rows and the page answered 500.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BotUser, Payment
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness


async def failing_suggestion(session: AsyncSession, **_kwargs: object) -> list:
    await session.execute(text("SELECT * FROM no_such_table"))
    return []


class ReceiptSuggestionFailureTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with async_sessionmaker(self.engine)() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="u1", role="user"))
            await session.flush()
            session.add_all([
                Payment(id=71, user_id=1, amount=1000, method="card", status="pending", receipt_file_id="f71"),
                Payment(id=72, user_id=1, amount=2000, method="card", status="pending", receipt_file_id="f72"),
            ])
            await session.commit()

    async def test_a_failed_suggestion_still_shows_every_payment(self) -> None:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}"
        with patch("app.services.receipt_matching.suggest_receipt_matches", failing_suggestion):
            response = await self.client.get("/finance?tab=payments", headers={"cookie": cookie})
        self.assertEqual(response.status_code, 200)
        self.assertIn("#71", response.text)
        self.assertIn("#72", response.text)
