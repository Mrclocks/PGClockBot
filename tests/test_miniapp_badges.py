"""Mini App home badges count only the customer's own items in the Mini App's own shop."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, LoyaltyReward, ShopPoints, Ticket
from app.services import miniapp_badges
from app.services.customer_loyalty import count_affordable_rewards
from app.services.customer_support import count_answered_tickets
from app.services.loyalty import SETTING_LOYALTY_ENABLED
from app.services.miniapp_badges import HomeBadges, home_badges
from app.services.users import clear_settings_cache, set_settings_bulk

SHOP, OTHER_SHOP = 5, 6
CUSTOMER, OTHER_CUSTOMER = 10, 11


class BadgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=5005, referral_code="o5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=6006, referral_code="o6", role="reseller"),
                BotUser(id=CUSTOMER, telegram_id=1010, referral_code="c10", role="user", points_balance=500),
                BotUser(id=OTHER_CUSTOMER, telegram_id=1011, referral_code="c11", role="user", points_balance=500),
            ])
            tickets = [
                (CUSTOMER, None, "answered"), (CUSTOMER, None, "answered"), (CUSTOMER, None, "open"),
                (CUSTOMER, None, "closed"), (CUSTOMER, SHOP, "answered"), (OTHER_CUSTOMER, None, "answered"),
                (CUSTOMER, OTHER_SHOP, "answered"),
            ]
            session.add_all([Ticket(user_id=u, reseller_id=r, subject="s", status=st) for u, r, st in tickets])
            rewards = [
                (None, 100, True, False), (None, 500, True, False), (None, 501, True, False),
                (None, 0, True, False), (None, 100, False, False), (None, 100, True, True),
                (SHOP, 100, True, False), (OTHER_SHOP, 10, True, False),
            ]
            session.add_all([
                LoyaltyReward(name="r", reward_type="wallet_credit", reward_value=1, points_cost=cost,
                              enabled=enabled, archived=archived, reseller_id=club)
                for club, cost, enabled, archived in rewards
            ])
            session.add(ShopPoints(user_id=CUSTOMER, reseller_id=SHOP, balance=150))
            await session.commit()
            self.customer = await session.get(BotUser, CUSTOMER)

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()

    async def test_answered_tickets_are_the_customers_own_at_this_desk(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await count_answered_tickets(session, CUSTOMER, shop_id=None), 2)
            self.assertEqual(await count_answered_tickets(session, CUSTOMER, shop_id=SHOP), 1)
            self.assertEqual(await count_answered_tickets(session, OTHER_CUSTOMER, shop_id=SHOP), 0)

    async def test_affordable_rewards_use_this_clubs_points_and_live_rewards(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await count_affordable_rewards(session, self.customer, shop_id=None), 2,
                             "cost 100 and 500 with 500 points; not 501, free, disabled, archived or another club's")
            self.assertEqual(await count_affordable_rewards(session, self.customer, shop_id=SHOP), 1,
                             "the shop's own points (150) and rewards")
            self.assertEqual(await count_affordable_rewards(session, self.customer, shop_id=OTHER_SHOP), 0,
                             "no points in that shop")

    async def test_a_closed_club_shows_no_reward_badge(self) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, {SETTING_LOYALTY_ENABLED: "0"})
            clear_settings_cache()
            self.assertEqual(await count_affordable_rewards(session, self.customer, shop_id=None), 0)

    async def test_home_badges_combine_both_and_fail_soft(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await home_badges(session, self.customer, shop_id=None), HomeBadges(support=2, club=2))
            broken = OperationalError("SELECT", {}, Exception("locked"))
            with patch.object(miniapp_badges, "count_answered_tickets", side_effect=broken):
                self.assertEqual(await home_badges(session, self.customer, shop_id=None), HomeBadges())
            self.assertEqual((await home_badges(session, self.customer, shop_id=SHOP)).payload(), {"support": 1, "club": 1})


if __name__ == "__main__":
    unittest.main()
