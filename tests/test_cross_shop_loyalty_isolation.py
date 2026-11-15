"""A customer of a shop and of the platform keeps two independent clubs.

Points, first-purchase rewards, referral rewards and referral stats earned on one side
never appear, count or get reversed on the other side.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import (
    BotUser,
    LoyaltyReward,
    LuckyWheelPrize,
    LuckyWheelSpin,
    Order,
    OrderStatus,
    Plan,
    PointsRule,
    PointsTransaction,
    ResellerProfile,
    RewardRedemption,
    Role,
)
from app.services.loyalty import (
    credit_points,
    ensure_loyalty_defaults,
    loyalty_enabled,
    on_order_delivered,
    overview_metrics,
    points_balance,
    redeem_reward,
    referral_stats,
    reverse_order_points,
)
from app.services.lucky_wheel import spin
from app.services.orders import _maybe_pay_referral_bonus
from app.services.users import (
    clear_settings_cache,
    get_or_create_user,
    reset_shop_reseller_id,
    set_setting,
    set_shop_reseller_id,
)
from app.services.wallet import get_wallet_balance

ROOT = Path(__file__).resolve().parents[1]
SHOP, OTHER_SHOP = 10, 11
PLATFORM_REFERRER, SHOP_REFERRER, CUSTOMER, STRANGER = 20, 21, 22, 23


class CrossShopCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'x.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=1010, referral_code="SHOP10", role=Role.RESELLER.value),
                BotUser(id=OTHER_SHOP, telegram_id=1011, referral_code="SHOP11", role=Role.RESELLER.value),
                BotUser(id=PLATFORM_REFERRER, telegram_id=1020, referral_code="PREF20"),
                BotUser(id=SHOP_REFERRER, telegram_id=1021, referral_code="SREF21", reseller_id=SHOP),
                # Referred on the platform, later also a customer of SHOP.
                BotUser(id=CUSTOMER, telegram_id=1022, referral_code="CUST22", referred_by_id=PLATFORM_REFERRER),
                BotUser(id=STRANGER, telegram_id=1023, referral_code="STRA23", reseller_id=OTHER_SHOP),
            ])
            await session.flush()
            session.add_all([ResellerProfile(user_id=SHOP, is_active=True),
                             ResellerProfile(user_id=OTHER_SHOP, is_active=True)])
            await session.commit()
            for scope in (None, SHOP):
                await ensure_loyalty_defaults(session, reseller_id=scope)
                await set_setting(session, "loyalty_enabled", "1", reseller_id=scope or 0)
        self.orders = 0

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def deliver(self, session, user_id: int, shop: int | None, *, amount: int = 50_000) -> Order:
        self.orders += 1
        plan = Plan(name=f"p{self.orders}", price=amount, duration_days=30, data_limit_gb=10,
                    is_active=True, owner_reseller_id=shop)
        session.add(plan)
        await session.flush()
        order = Order(user_id=user_id, plan_id=plan.id, amount=amount,
                      status=OrderStatus.DELIVERED.value, reseller_id=shop)
        session.add(order)
        await session.commit()
        order.user = await session.get(BotUser, user_id)
        order.plan = plan
        await on_order_delivered(session, order)
        return order

    async def balance(self, user_id: int, shop: int | None) -> int:
        async with self.Session() as session:
            return await points_balance(session, await session.get(BotUser, user_id), shop)

    async def in_bot(self, shop: int | None, coro_factory):
        token = set_shop_reseller_id(shop)
        try:
            return await coro_factory()
        finally:
            reset_shop_reseller_id(token)


class ReferralRewardsStayInTheirShopTests(CrossShopCase):
    async def test_purchase_in_a_shop_does_not_reward_a_platform_referrer(self) -> None:
        async with self.Session() as session:
            await self.deliver(session, CUSTOMER, SHOP)
        self.assertEqual(await self.balance(PLATFORM_REFERRER, SHOP), 0)
        self.assertEqual(await self.balance(PLATFORM_REFERRER, None), 0)
        async with self.Session() as session:
            await self.deliver(session, CUSTOMER, None)
        self.assertGreater(await self.balance(PLATFORM_REFERRER, None), 0)
        self.assertEqual(await self.balance(PLATFORM_REFERRER, SHOP), 0)

    async def test_shop_referral_rewards_only_shop_purchases(self) -> None:
        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER)
            customer.referred_by_id, customer.referral_shop_id = SHOP_REFERRER, SHOP
            await session.commit()
            await self.deliver(session, CUSTOMER, None)
        self.assertEqual(await self.balance(SHOP_REFERRER, None), 0)
        self.assertEqual(await self.balance(SHOP_REFERRER, SHOP), 0)
        async with self.Session() as session:
            await self.deliver(session, CUSTOMER, SHOP)
        self.assertGreater(await self.balance(SHOP_REFERRER, SHOP), 0)
        self.assertEqual(await self.balance(SHOP_REFERRER, None), 0)

    async def test_wallet_referral_bonus_is_paid_only_by_the_referral_shop(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "referral_bonus", "7000", reseller_id=SHOP)
            await set_setting(session, "referral_bonus", "3000", reseller_id=0)
            shop_order = Order(user_id=CUSTOMER, amount=50_000, status=OrderStatus.DELIVERED.value, reseller_id=SHOP)
            session.add(shop_order)
            await session.commit()
            shop_order.user = await session.get(BotUser, CUSTOMER)
            await _maybe_pay_referral_bonus(session, shop_order)
            await session.commit()
            referrer = await session.get(BotUser, PLATFORM_REFERRER)
            self.assertEqual(await get_wallet_balance(session, referrer, shop_id=SHOP), 0)
            self.assertEqual(await get_wallet_balance(session, referrer, shop_id=None), 0)

            platform_order = Order(user_id=CUSTOMER, amount=50_000, status=OrderStatus.DELIVERED.value)
            session.add(platform_order)
            await session.commit()
            platform_order.user = await session.get(BotUser, CUSTOMER)
            await _maybe_pay_referral_bonus(session, platform_order)
            await session.commit()
            self.assertEqual(await get_wallet_balance(session, referrer, shop_id=None), 3000)
            self.assertEqual(await get_wallet_balance(session, referrer, shop_id=SHOP), 0)

    async def test_platform_bonus_setting_is_read_even_inside_a_shop_bot(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "referral_bonus", "5000", reseller_id=SHOP)
            order = Order(user_id=CUSTOMER, amount=50_000, status=OrderStatus.DELIVERED.value)
            session.add(order)
            await session.commit()
            order.user = await session.get(BotUser, CUSTOMER)

            async def pay():
                await _maybe_pay_referral_bonus(session, order)
                await session.commit()

            await self.in_bot(SHOP, pay)
            referrer = await session.get(BotUser, PLATFORM_REFERRER)
            self.assertEqual(await get_wallet_balance(session, referrer, shop_id=None), 0)

    async def test_referral_stats_show_each_clubs_invites_only(self) -> None:
        async with self.Session() as session:
            session.add(BotUser(id=30, telegram_id=1030, referral_code="NEW30",
                                referred_by_id=PLATFORM_REFERRER, referral_shop_id=SHOP, reseller_id=SHOP))
            await session.commit()
            await self.deliver(session, CUSTOMER, None)
            await self.deliver(session, 30, SHOP)
            platform = await referral_stats(session, PLATFORM_REFERRER, shop_id=None)
            shop = await referral_stats(session, PLATFORM_REFERRER, shop_id=SHOP)
            other = await referral_stats(session, PLATFORM_REFERRER, shop_id=OTHER_SHOP)
        self.assertEqual((platform["total"], platform["qualified"]), (1, 1))
        self.assertEqual((shop["total"], shop["qualified"]), (1, 1))
        self.assertEqual(platform["earned_points"], await self.balance(PLATFORM_REFERRER, None))
        self.assertEqual(shop["earned_points"], await self.balance(PLATFORM_REFERRER, SHOP))
        self.assertEqual(other, {"total": 0, "qualified": 0, "earned_points": 0})

    async def test_staff_overview_counts_each_shops_referrals_only(self) -> None:
        async with self.Session() as session:
            session.add(BotUser(id=30, telegram_id=1030, referral_code="NEW30",
                                referred_by_id=SHOP_REFERRER, referral_shop_id=SHOP, reseller_id=SHOP))
            await session.commit()
            platform = await overview_metrics(session, reseller_id=None)
            shop = await overview_metrics(session, reseller_id=SHOP)
        self.assertEqual(platform["total_referrals"], 1)
        self.assertEqual([row["user_id"] for row in platform["top_referrers"]], [PLATFORM_REFERRER])
        self.assertEqual(shop["total_referrals"], 1)
        self.assertEqual([row["user_id"] for row in shop["top_referrers"]], [SHOP_REFERRER])


class PurchaseRewardsPerShopTests(CrossShopCase):
    async def first_purchase_points(self, shop: int | None) -> int:
        async with self.Session() as session:
            return int(await session.scalar(
                select(sa.func.coalesce(sa.func.sum(PointsTransaction.amount), 0)).join(
                    PointsRule, PointsTransaction.source == sa.func.concat("rule:", PointsRule.id)
                ).where(
                    PointsTransaction.user_id == CUSTOMER,
                    PointsRule.event_key == "first_purchase",
                    PointsTransaction.reseller_id == shop if shop else PointsTransaction.reseller_id.is_(None),
                )
            ) or 0)

    async def test_first_purchase_in_a_shop_counts_after_platform_purchases(self) -> None:
        async with self.Session() as session:
            await self.deliver(session, CUSTOMER, None)
            await self.deliver(session, CUSTOMER, SHOP)
        self.assertGreater(await self.first_purchase_points(None), 0)
        self.assertGreater(await self.first_purchase_points(SHOP), 0)

    async def test_a_rule_the_shop_switched_off_is_not_replaced_by_the_platforms(self) -> None:
        async with self.Session() as session:
            rules = await session.scalars(select(PointsRule).where(PointsRule.reseller_id == SHOP))
            for rule in rules:
                rule.enabled = False
            await session.commit()
            await self.deliver(session, CUSTOMER, SHOP)
        self.assertEqual(await self.balance(CUSTOMER, SHOP), 0)
        self.assertEqual(await self.balance(CUSTOMER, None), 0)

    async def test_refund_reverses_only_that_orders_points(self) -> None:
        async with self.Session() as session:
            order = await self.deliver(session, CUSTOMER, None)
            earned = await points_balance(session, await session.get(BotUser, CUSTOMER), None)
            stranger = await session.get(BotUser, STRANGER)
            # Another customer's lucky-wheel win in another shop stores the same number as reference.
            await credit_points(session, stranger, 40, tx_type="earn", source="lucky_wheel",
                                reference=str(order.id), description="wheel", idempotency_key="w1",
                                shop_id=OTHER_SHOP)
            await reverse_order_points(session, order, reason="refund")
        self.assertGreater(earned, 0)
        self.assertEqual(await self.balance(CUSTOMER, None), 0)
        self.assertEqual(await self.balance(STRANGER, OTHER_SHOP), 40)

    async def test_club_switch_for_the_platform_is_not_read_from_the_shop_bot(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "0", reseller_id=0)

            async def platform_enabled() -> bool:
                return await loyalty_enabled(session, reseller_id=None)

            self.assertFalse(await self.in_bot(SHOP, platform_enabled))


class RegistrationRecordsTheReferralShopTests(CrossShopCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.config_patch = patch("app.services.users.get_settings",
                                  return_value=SimpleNamespace(admin_ids=[], bot_token="main"))
        self.config_patch.start()

    async def asyncTearDown(self) -> None:
        self.config_patch.stop()
        await super().asyncTearDown()

    async def test_a_shop_registration_records_the_shop_and_a_platform_one_none(self) -> None:
        async with self.Session() as session:
            in_shop = await get_or_create_user(session, 5001, referred_by_code="SREF21", reseller_owner_id=SHOP)
            on_platform = await get_or_create_user(session, 5002, referred_by_code="PREF20")
            plain = await get_or_create_user(session, 5003, reseller_owner_id=SHOP)
        self.assertEqual((in_shop.referred_by_id, in_shop.referral_shop_id), (SHOP_REFERRER, SHOP))
        self.assertEqual((on_platform.referred_by_id, on_platform.referral_shop_id), (PLATFORM_REFERRER, None))
        self.assertEqual((plain.referred_by_id, plain.referral_shop_id), (None, None))

    async def test_a_member_of_the_shop_can_invite_in_its_bot_but_a_foreign_customer_cannot(self) -> None:
        async with self.Session() as session:
            from app.services.shop_membership import ensure_member

            await ensure_member(session, CUSTOMER, SHOP)
            await session.commit()
            invited = await get_or_create_user(session, 5004, referred_by_code="CUST22", reseller_owner_id=SHOP)
            refused = await get_or_create_user(session, 5005, referred_by_code="STRA23", reseller_owner_id=SHOP)
        self.assertEqual((invited.referred_by_id, invited.referral_shop_id), (CUSTOMER, SHOP))
        self.assertIsNone(refused.referred_by_id)


class ReplaysStayInTheirClubTests(CrossShopCase):
    async def test_a_wheel_slot_used_in_another_club_is_not_replayed(self) -> None:
        async with self.Session() as session:
            for scope in (0, SHOP):
                for key, value in (("lucky_wheel_enabled", "1"), ("lucky_wheel_spin_cost_points", "0"),
                                   ("lucky_wheel_daily_limit", "10"), ("lucky_wheel_cooldown_seconds", "0")):
                    await set_setting(session, key, value, reseller_id=scope)
            session.add_all([
                LuckyWheelPrize(reseller_id=None, label="platform prize", prize_type="none", prize_value=0, weight=1),
                LuckyWheelPrize(reseller_id=SHOP, label="shop prize", prize_type="none", prize_value=0, weight=1),
            ])
            await session.commit()
            customer = await session.get(BotUser, CUSTOMER)
            first = await self.in_bot(None, lambda: spin(session, customer, idempotency_key="tg:wheel:22:0"))
            self.assertEqual(first.prize_label, "platform prize")
            with self.assertRaises(ValueError):
                await self.in_bot(SHOP, lambda: spin(session, customer, idempotency_key="tg:wheel:22:0"))
            stranger = await session.get(BotUser, STRANGER)
            with self.assertRaises(ValueError):
                await self.in_bot(None, lambda: spin(session, stranger, idempotency_key="tg:wheel:22:0"))
            spins = await session.scalar(select(sa.func.count()).select_from(LuckyWheelSpin))
        self.assertEqual(spins, 1)

    async def test_a_redemption_is_not_replayed_to_another_club_or_customer(self) -> None:
        async with self.Session() as session:
            session.add(LoyaltyReward(id=70, name="credit", reward_type="wallet_credit", reward_value=100,
                                      points_cost=10, enabled=True, reseller_id=None))
            await session.commit()
            customer = await session.get(BotUser, CUSTOMER)
            await credit_points(session, customer, 10, tx_type="earn", source="t", reference=None,
                                description="t", idempotency_key="seed", shop_id=None)
            await self.in_bot(None, lambda: redeem_reward(session, customer, 70, idempotency_key="slot"))
            with self.assertRaises(ValueError):
                await self.in_bot(SHOP, lambda: redeem_reward(session, customer, 70, idempotency_key="slot"))
            stranger = await session.get(BotUser, STRANGER)
            with self.assertRaises(ValueError):
                await self.in_bot(None, lambda: redeem_reward(session, stranger, 70, idempotency_key="slot"))
            redemptions = await session.scalar(select(sa.func.count()).select_from(RewardRedemption))
        self.assertEqual(redemptions, 1)


class ReferralShopMigrationTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("referral_shop", ROOT / "alembic/versions/0111_referral_shop.py")
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        return migration

    def test_upgrade_twice_backfills_shop_referrals_and_downgrade_keeps_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = self.load()
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE bot_users (id INTEGER PRIMARY KEY, referred_by_id INTEGER, reseller_id INTEGER)"
            ))
            conn.execute(sa.text(
                "INSERT INTO bot_users (id, referred_by_id, reseller_id) VALUES "
                "(10, NULL, NULL), (20, NULL, NULL), (21, NULL, 10), "
                "(30, 21, 10), (31, 10, 10), (32, 20, 10), (33, 20, NULL), (34, NULL, 10)"
            ))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("bot_users")}
                self.assertIn("referral_shop_id", columns)
                rows = dict(conn.execute(sa.text("SELECT id, referral_shop_id FROM bot_users")).all())
                # Shop customer or owner as referrer -> that shop; platform referrer -> platform.
                self.assertEqual((rows[30], rows[31], rows[32], rows[33], rows[34]), (10, 10, None, None, None))
                migration.downgrade()
                migration.downgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("bot_users")}
                self.assertNotIn("referral_shop_id", columns)
                self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM bot_users")).scalar(), 8)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
