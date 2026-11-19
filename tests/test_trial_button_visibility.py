"""The shop's trial button can be hidden from customers who already took their trial."""

from __future__ import annotations

import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers.shop import _shop_kind_flags
from app.db.models import Base, BotUser, Plan, TrialClaim
from app.services.users import clear_settings_cache, set_settings_bulk
from tests.postgres_support import scoped_async_engine


class TrialButtonVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.user = BotUser(id=1, telegram_id=11, referral_code="a", role="user")
        self.session.add_all([
            self.user,
            Plan(id=1, name="paid", price=100, is_active=True),
            Plan(id=2, name="trial", price=0, is_active=True, is_trial=True),
            TrialClaim(user_id=1, shop_key="platform", telegram_id=11),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        clear_settings_cache()

    async def trial_shown(self, **settings: str) -> bool:
        await set_settings_bulk(self.session, {"trial_enabled": "1", **settings})
        clear_settings_cache()
        _ui, _fixed, trial_on, *_rest = await _shop_kind_flags(self.session, self.user)
        return trial_on

    async def test_claimed_customer_still_sees_trial_by_default(self) -> None:
        self.assertTrue(await self.trial_shown())

    async def test_claimed_customer_does_not_see_trial_when_switched_off(self) -> None:
        self.assertFalse(await self.trial_shown(trial_show_claimed="0"))

    async def test_eligible_customer_sees_trial_when_switched_off(self) -> None:
        claim = await self.session.scalar(select(TrialClaim).where(TrialClaim.user_id == 1))
        await self.session.delete(claim)
        await self.session.commit()
        self.assertTrue(await self.trial_shown(trial_show_claimed="0"))


if __name__ == "__main__":
    unittest.main()
