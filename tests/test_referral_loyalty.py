"""Focused tests for Referral + Loyalty / Points engine."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

# Force sqlite for unit tests
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class LoyaltyEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "loy.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, tid: int, code: str, *, referred_by=None):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tid,
            role=Role.USER.value,
            referral_code=code,
            referred_by_id=referred_by,
            wallet_balance=0,
            points_balance=0,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def test_credit_debit_idempotent_and_balance(self):
        from app.services.loyalty import credit_points, debit_points, ensure_loyalty_defaults

        async with self.Session() as session:
            await ensure_loyalty_defaults(session)
            u = await self._user(session, 1001, "AAAA1111")
            t1 = await credit_points(
                session,
                u,
                50,
                tx_type="earn",
                source="test",
                reference="1",
                description="first",
                idempotency_key="k1",
                shop_id=None,
            )
            t2 = await credit_points(
                session,
                u,
                50,
                tx_type="earn",
                source="test",
                reference="1",
                description="first",
                idempotency_key="k1",
                shop_id=None,
            )
            self.assertEqual(t1.id, t2.id)
            await session.refresh(u)
            self.assertEqual(u.points_balance, 50)

            await debit_points(
                session,
                u,
                20,
                tx_type="spend",
                source="test",
                reference="2",
                description="spend",
                idempotency_key="k2",
                shop_id=None,
            )
            await session.refresh(u)
            self.assertEqual(u.points_balance, 30)

            with self.assertRaises(ValueError):
                await debit_points(
                    session,
                    u,
                    999,
                    tx_type="spend",
                    source="test",
                    reference="3",
                    description="over",
                    idempotency_key="k3",
                    shop_id=None,
                )

    async def test_self_referral_blocked_on_event(self):
        from app.services.loyalty import record_referral_event

        async with self.Session() as session:
            u = await self._user(session, 2001, "BBBB2222")
            ev = await record_referral_event(
                session,
                referrer_id=u.id,
                referred_id=u.id,
                event_key="referral_signup",
                idempotency_key="self1",
            )
            self.assertIsNone(ev)

    async def test_duplicate_referral_event_idempotent(self):
        from app.services.loyalty import record_referral_event

        async with self.Session() as session:
            a = await self._user(session, 3001, "CCCC3333")
            b = await self._user(session, 3002, "DDDD4444", referred_by=a.id)
            e1 = await record_referral_event(
                session,
                referrer_id=a.id,
                referred_id=b.id,
                event_key="referral_signup",
                idempotency_key="dup1",
            )
            await session.commit()
            e2 = await record_referral_event(
                session,
                referrer_id=a.id,
                referred_id=b.id,
                event_key="referral_signup",
                idempotency_key="dup1",
            )
            self.assertEqual(e1.id, e2.id)

    async def test_order_delivery_awards_and_duplicate_safe(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.loyalty import ensure_loyalty_defaults, on_order_delivered

        async with self.Session() as session:
            await ensure_loyalty_defaults(session)
            ref = await self._user(session, 4001, "EEEE5555")
            buyer = await self._user(session, 4002, "FFFF6666", referred_by=ref.id)
            plan = Plan(name="P", price=10000, duration_days=30, data_limit_gb=10, is_active=True)
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            order = Order(
                user_id=buyer.id,
                plan_id=plan.id,
                amount=10000,
                status=OrderStatus.DELIVERED.value,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            order.user = buyer
            order.plan = plan

            await on_order_delivered(session, order)
            await on_order_delivered(session, order)  # duplicate callback
            await session.refresh(buyer)
            await session.refresh(ref)
            self.assertGreater(buyer.points_balance, 0)
            self.assertGreater(ref.points_balance, 0)
            buyer_pts = buyer.points_balance
            ref_pts = ref.points_balance
            await on_order_delivered(session, order)
            await session.refresh(buyer)
            await session.refresh(ref)
            self.assertEqual(buyer.points_balance, buyer_pts)
            self.assertEqual(ref.points_balance, ref_pts)

    async def test_wallet_credit_redeem_atomic(self):
        from app.db.models import LoyaltyReward
        from app.services.loyalty import credit_points, ensure_loyalty_defaults, redeem_reward

        async with self.Session() as session:
            await ensure_loyalty_defaults(session)
            u = await self._user(session, 5001, "GGGG7777")
            await credit_points(
                session,
                u,
                500,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed500",
                shop_id=None,
            )
            reward = LoyaltyReward(
                name="کیف",
                reward_type="wallet_credit",
                reward_value=50000,
                points_cost=500,
                enabled=True,
                archived=False,
            )
            session.add(reward)
            await session.commit()
            await session.refresh(reward)
            red = await redeem_reward(
                session, u, reward.id, idempotency_key="redeem-once"
            )
            red2 = await redeem_reward(
                session, u, reward.id, idempotency_key="redeem-once"
            )
            self.assertEqual(red.id, red2.id)
            await session.refresh(u)
            self.assertEqual(u.points_balance, 0)
            self.assertEqual(u.wallet_balance, 50000)

            with self.assertRaises(ValueError):
                await redeem_reward(
                    session, u, reward.id, idempotency_key="redeem-again"
                )

    async def test_disabled_reward_cannot_redeem(self):
        from app.db.models import LoyaltyReward
        from app.services.loyalty import credit_points, redeem_reward

        async with self.Session() as session:
            u = await self._user(session, 6001, "HHHH8888")
            await credit_points(
                session,
                u,
                200,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed200",
                shop_id=None,
            )
            reward = LoyaltyReward(
                name="off",
                reward_type="wallet_credit",
                reward_value=1000,
                points_cost=100,
                enabled=False,
                archived=False,
            )
            session.add(reward)
            await session.commit()
            await session.refresh(reward)
            with self.assertRaises(ValueError):
                await redeem_reward(session, u, reward.id, idempotency_key="x1")

    async def test_admin_adjust_requires_reason(self):
        from app.services.loyalty import admin_adjust_points, credit_points

        async with self.Session() as session:
            u = await self._user(session, 7001, "IIII9999")
            await credit_points(
                session,
                u,
                10,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed10",
                shop_id=None,
            )
            with self.assertRaises(ValueError):
                await admin_adjust_points(session, u, 5, reason="  ", admin_identity="admin", shop_id=None, request_key="k1")
            tx = await admin_adjust_points(
                session, u, 5, reason="جبران دستی", admin_identity="admin",
                shop_id=None, request_key="k2",
            )
            self.assertEqual(tx.tx_type, "adjust")
            await session.refresh(u)
            self.assertEqual(u.points_balance, 15)

    async def test_referral_link_uses_config_username(self):
        from app.services.loyalty import referral_link

        self.assertEqual(
            referral_link("MyBot", "ABC123"),
            "https://t.me/MyBot?start=ref_ABC123",
        )

    async def test_reverse_allows_negative_debt_policy(self):
        from app.services.loyalty import credit_points, debit_points, reverse_points_tx

        async with self.Session() as session:
            u = await self._user(session, 8001, "JJJJ0000")
            tx = await credit_points(
                session,
                u,
                100,
                tx_type="earn",
                source="test",
                reference="ord1",
                description="buy",
                idempotency_key="earn100",
                shop_id=None,
            )
            await debit_points(
                session,
                u,
                100,
                tx_type="redeem",
                source="reward:1",
                reference="1",
                description="spent",
                idempotency_key="spent100",
                shop_id=None,
            )
            await session.refresh(u)
            self.assertEqual(u.points_balance, 0)
            await reverse_points_tx(session, tx, reason="refund")
            await session.refresh(u)
            self.assertEqual(u.points_balance, -100)

    async def test_discount_redeem_and_checkout_apply(self):
        from sqlalchemy import select

        from app.db.models import LoyaltyDiscountEntitlement, LoyaltyReward, Order, OrderStatus
        from app.services.loyalty import credit_points, redeem_reward
        from app.services.orders import apply_discount_to_order

        async with self.Session() as session:
            u = await self._user(session, 9001, "KKKK1111")
            await credit_points(
                session,
                u,
                300,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed300",
                shop_id=None,
            )
            reward = LoyaltyReward(
                name="۱۵٪",
                reward_type="discount_percent",
                reward_value=15,
                points_cost=300,
                enabled=True,
                archived=False,
                min_purchase_toman=0,
                max_discount_toman=50000,
                expires_days=7,
            )
            session.add(reward)
            await session.commit()
            await session.refresh(reward)

            red = await redeem_reward(session, u, reward.id, idempotency_key="disc1")
            self.assertTrue(red.discount_code)
            self.assertTrue(red.discount_code.startswith("LOY"))
            await session.refresh(u)
            self.assertEqual(u.points_balance, 0)

            ent = (
                await session.execute(
                    select(LoyaltyDiscountEntitlement).where(
                        LoyaltyDiscountEntitlement.code == red.discount_code
                    )
                )
            ).scalar_one()
            self.assertEqual(ent.status, "available")
            self.assertEqual(ent.user_id, u.id)

            order = Order(
                user_id=u.id,
                amount=200000,
                discount_amount=0,
                status=OrderStatus.PENDING.value,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)

            order = await apply_discount_to_order(session, order, red.discount_code)
            # 15% of 200000 = 30000, under max 50000
            self.assertEqual(order.discount_amount, 30000)
            self.assertEqual(order.amount, 170000)
            await session.refresh(ent)
            self.assertEqual(ent.status, "reserved")

            # Other user cannot use
            u2 = await self._user(session, 9002, "LLLL2222")
            order2 = Order(
                user_id=u2.id,
                amount=100000,
                status=OrderStatus.PENDING.value,
            )
            session.add(order2)
            await session.commit()
            await session.refresh(order2)
            with self.assertRaises(ValueError):
                await apply_discount_to_order(session, order2, red.discount_code)

            # Duplicate apply on same order blocked (already has discount)
            with self.assertRaises(ValueError):
                await apply_discount_to_order(session, order, red.discount_code)

    async def test_discount_min_max_and_release(self):
        from sqlalchemy import select

        from app.db.models import LoyaltyDiscountEntitlement, LoyaltyReward, Order, OrderStatus
        from app.services.loyalty import (
            consume_loyalty_discount_for_order,
            credit_points,
            redeem_reward,
            release_loyalty_discount_for_order,
        )
        from app.services.orders import apply_discount_to_order

        async with self.Session() as session:
            u = await self._user(session, 9101, "MMMM3333")
            await credit_points(
                session,
                u,
                300,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed300b",
                shop_id=None,
            )
            reward = LoyaltyReward(
                name="۱۰٪",
                reward_type="discount_percent",
                reward_value=10,
                points_cost=300,
                enabled=True,
                archived=False,
                min_purchase_toman=50000,
                max_discount_toman=5000,
                expires_days=7,
            )
            session.add(reward)
            await session.commit()
            await session.refresh(reward)
            red = await redeem_reward(session, u, reward.id, idempotency_key="disc2")

            small = Order(
                user_id=u.id, amount=10000, status=OrderStatus.PENDING.value
            )
            session.add(small)
            await session.commit()
            await session.refresh(small)
            with self.assertRaises(ValueError):
                await apply_discount_to_order(session, small, red.discount_code)

            order = Order(
                user_id=u.id, amount=100000, status=OrderStatus.PENDING.value
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            order = await apply_discount_to_order(session, order, red.discount_code)
            # 10% = 10000 capped to 5000
            self.assertEqual(order.discount_amount, 5000)
            self.assertEqual(order.amount, 95000)

            await release_loyalty_discount_for_order(session, order)
            await session.commit()
            ent = (
                await session.execute(
                    select(LoyaltyDiscountEntitlement).where(
                        LoyaltyDiscountEntitlement.code == red.discount_code
                    )
                )
            ).scalar_one()
            self.assertEqual(ent.status, "available")

            # Re-apply and consume
            order.discount_code = None
            order.discount_amount = 0
            order.amount = 100000
            await session.commit()
            order = await apply_discount_to_order(session, order, red.discount_code)
            await consume_loyalty_discount_for_order(session, order)
            await session.commit()
            await session.refresh(ent)
            self.assertEqual(ent.status, "consumed")

    async def test_traffic_and_time_call_pasarguard(self):
        from unittest.mock import AsyncMock, patch

        from app.db.models import LoyaltyReward, UserService
        from app.services.loyalty import credit_points, redeem_reward

        async with self.Session() as session:
            u = await self._user(session, 9201, "NNNN4444")
            await credit_points(
                session,
                u,
                500,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed500b",
                shop_id=None,
            )
            svc = UserService(
                bot_user_id=u.id,
                pg_user_id=77,
                pg_username="u77",
            )
            session.add(svc)
            traffic = LoyaltyReward(
                name="۵گ",
                reward_type="traffic_gb",
                reward_value=5,
                points_cost=100,
                enabled=True,
                archived=False,
            )
            time_r = LoyaltyReward(
                name="۷روز",
                reward_type="time_days",
                reward_value=7,
                points_cost=150,
                enabled=True,
                archived=False,
            )
            session.add_all([traffic, time_r])
            await session.commit()
            await session.refresh(svc)
            await session.refresh(traffic)
            await session.refresh(time_r)

            mock_pg = AsyncMock()
            mock_pg.get_user_by_id = AsyncMock(
                return_value={"data_limit": 10 * (1024**3), "expire": 2000000000}
            )
            mock_pg.modify_user_by_id = AsyncMock(return_value={})
            with patch("app.services.pasarguard.get_pg", return_value=mock_pg):
                await redeem_reward(
                    session, u, traffic.id, service_id=svc.id, idempotency_key="tr1"
                )
                await redeem_reward(
                    session, u, time_r.id, service_id=svc.id, idempotency_key="tm1"
                )
            self.assertEqual(mock_pg.modify_user_by_id.await_count, 2)
            traffic_payload = mock_pg.modify_user_by_id.await_args_list[0].args[1]
            self.assertEqual(
                traffic_payload["data_limit"], 15 * (1024**3)
            )
            time_payload = mock_pg.modify_user_by_id.await_args_list[1].args[1]
            self.assertGreater(time_payload["expire"], 2000000000)

            # Duplicate traffic idempotent
            with patch("app.services.pasarguard.get_pg", return_value=mock_pg):
                await redeem_reward(
                    session, u, traffic.id, service_id=svc.id, idempotency_key="tr1"
                )
            self.assertEqual(mock_pg.modify_user_by_id.await_count, 2)


class LoyaltyStaticContractTests(unittest.TestCase):
    def test_handlers_and_panel_wired(self):
        from pathlib import Path

        bot_init = Path("app/bot/__init__.py").read_text()
        self.assertIn("loyalty", bot_init)
        app_py = Path("app/api/app.py").read_text()
        self.assertIn("register_loyalty_pages", app_py)
        from app.services.marketing_nav import marketing_sections

        owner = {
            "role": "admin",
            "permissions": [],
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }
        paths = {section.href.split("?")[0] for section in marketing_sections(owner)}
        self.assertIn("/loyalty", paths)
        orders = Path("app/services/orders.py").read_text()
        self.assertIn("on_order_delivered", orders)
        models = Path("app/db/models.py").read_text()
        self.assertIn("points_balance", models)
        self.assertIn("class PointsTransaction", models)
        mig = Path("alembic/versions/0007_referral_loyalty.py").read_text()
        self.assertIn("0007_referral_loyalty", mig)

    def test_no_owner_pg_fallback_on_reseller_reward(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from app.services.loyalty import apply_service_reward
        from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

        service = SimpleNamespace(id=1, pg_user_id=9, bot_user_id=1)
        user = SimpleNamespace(id=1)

        async def apply(service_shop: int | None, bot_shop: int | None):
            token = set_shop_reseller_id(bot_shop)
            try:
                with (
                    patch("app.services.service_cancellations.lock_service_mutation", AsyncMock()),
                    patch("app.services.loyalty.service_shop_id", AsyncMock(return_value=service_shop)),
                    patch(
                        "app.services.pasarguard.get_pg_for_reseller",
                        AsyncMock(side_effect=ValueError("no panel login")),
                    ),
                    patch("app.services.pasarguard.get_pg", side_effect=AssertionError("owner client used")),
                ):
                    await apply_service_reward(AsyncMock(), user, service, "time_days", 1)
            finally:
                reset_shop_reseller_id(token)

        # A shop service without a usable shop panel login never falls back to the Owner client.
        with self.assertRaisesRegex(ValueError, "no panel login"):
            asyncio.run(apply(5, 5))
        # A reward cannot be applied across shops, in either direction.
        for service_shop, bot_shop in ((None, 5), (5, None), (6, 5)):
            with self.assertRaisesRegex(ValueError, "متعلق"):
                asyncio.run(apply(service_shop, bot_shop))

    def test_panel_shop_scope_helpers(self):
        from app.api.loyalty_pages import _reward_in_scope, _rule_in_scope, _shop_scope
        from app.db.models import LoyaltyReward, PointsRule

        owner = {
            "role": "admin",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }
        self.assertIsNone(_shop_scope(owner))
        with self.assertRaises(ValueError):
            _shop_scope({"role": "admin"})
        self.assertEqual(_shop_scope({"role": "reseller", "bot_user_id": 42}), 42)
        with self.assertRaises(ValueError):
            _shop_scope({"role": "reseller"})
        with self.assertRaises(ValueError):
            _shop_scope({"role": "pg_staff", "bot_user_id": 1})

        platform_rule = PointsRule(
            name="p", event_key="purchase", amount_mode="fixed", amount=1, reseller_id=None
        )
        shop_rule = PointsRule(
            name="s", event_key="purchase", amount_mode="fixed", amount=1, reseller_id=42
        )
        self.assertTrue(_rule_in_scope(platform_rule, None))
        self.assertFalse(_rule_in_scope(shop_rule, None))
        self.assertTrue(_rule_in_scope(shop_rule, 42))
        self.assertFalse(_rule_in_scope(platform_rule, 42))

        platform_rw = LoyaltyReward(
            name="p",
            reward_type="wallet_credit",
            reward_value=1,
            points_cost=1,
            reseller_id=None,
        )
        shop_rw = LoyaltyReward(
            name="s",
            reward_type="wallet_credit",
            reward_value=1,
            points_cost=1,
            reseller_id=42,
        )
        self.assertTrue(_reward_in_scope(platform_rw, None))
        self.assertFalse(_reward_in_scope(shop_rw, None))
        self.assertTrue(_reward_in_scope(shop_rw, 42))
        self.assertFalse(_reward_in_scope(platform_rw, 42))

    def test_tier_save_requires_admin(self):
        from pathlib import Path

        src = Path("app/api/loyalty_pages.py").read_text()
        self.assertIn('async def loyalty_tier_save', src)
        # Tier mutation must use require_admin, not require_loyalty
        idx = src.index("async def loyalty_tier_save")
        chunk = src[idx : idx + 250]
        self.assertIn("Depends(require_admin)", chunk)

    def test_wallet_commit_flag(self):
        import inspect
        from app.services.wallet import credit_wallet, debit_wallet

        self.assertIn("commit", inspect.signature(credit_wallet).parameters)
        self.assertIn("commit", inspect.signature(debit_wallet).parameters)

    def test_feature_perm_loyalty(self):
        from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS

        keys = {k for k, _ in FEATURE_PERMS}
        self.assertIn("loyalty", keys)
        self.assertIn("loyalty", DEFAULT_FEATURE_PERMS)

    def test_loyalty_perm_does_not_imply_other_shop_keys(self):
        from app.services.authz import authz_from_shop_perm_list, can_shop

        ctx = authz_from_shop_perm_list(["loyalty"], role="reseller")
        self.assertTrue(can_shop(ctx, "loyalty"))
        self.assertFalse(can_shop(ctx, "orders"))
        self.assertFalse(can_shop(ctx, "payments"))
        self.assertFalse(can_shop(ctx, "plans"))


if __name__ == "__main__":
    unittest.main()
