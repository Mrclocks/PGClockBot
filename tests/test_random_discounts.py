"""Random checkout prices remain bounded, isolated and fixed through payment retries."""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.models import (
    Base, BotUser, DiscountCode, Order, Payment, Plan, RandomDiscount, RandomDiscountDraw,
    ServiceAddonPack, UserService,
)
from app.services.discount_checkout import start_discounted_checkout
from app.services.checkout_summary import checkout_summary
from app.services.formatting import format_toman
from app.services.orders import (
    _release_order_discount, apply_discount_to_order, cancel_order, create_custom_order,
    create_order, create_wholesale_order, renew_service_with_plan, start_method_payment,
)
from app.services.random_discounts import (
    RandomDiscountInput, apply_random_discount, create_random_discount, stop_random_discount,
)
from app.services.service_addons import create_addon_order
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id

OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP = {"role": "reseller", "bot_user_id": 2, "permissions": ["random_discounts"]}


class RandomDiscountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        test_url = os.environ.get("PGCLOCKBOT_RANDOM_TEST_URL")
        self.admin_engine = None
        if test_url:
            self.schema = "random_test_" + secrets.token_hex(8)
            self.admin_engine = create_async_engine(test_url)
            async with self.admin_engine.begin() as connection:
                await connection.execute(CreateSchema(self.schema))
            self.engine = create_async_engine(test_url, connect_args={"server_settings": {"search_path": self.schema}})
        else:
            self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.tmp.name}/random.db")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        self.session.add_all([
            BotUser(id=i, telegram_id=100 + i, referral_code=f"random{i}", role="reseller" if i in (2, 3) else "user")
            for i in range(1, 16)
        ])
        await self.session.flush()
        self.plan = Plan(id=1, name="پلن", price=1000, duration_days=30, data_limit_gb=10)
        self.pack = ServiceAddonPack(id=1, name="حجم", kind="volume", amount=2, price=1200)
        self.service = UserService(id=1, bot_user_id=1, plan_id=1, pg_user_id=10, pg_username="random_buyer")
        self.session.add_all([
            self.plan, self.pack, self.service,
            Plan(id=2, name="فروشگاه", price=1000, owner_reseller_id=2),
            Plan(id=3, name="فروشگاه دیگر", price=1000, owner_reseller_id=3),
            Plan(id=4, name="تست", price=0, is_trial=True),
        ])
        await self.session.flush()
        if test_url:
            await self.session.execute(text("SELECT setval(pg_get_serial_sequence('plans', 'id'), 4)"))
        await self.session.commit()
        self.contact = patch("app.services.orders.require_purchase_contact", AsyncMock())
        self.contact.start()

    async def asyncTearDown(self) -> None:
        self.contact.stop()
        await self.session.close()
        await self.engine.dispose()
        if self.admin_engine:
            async with self.admin_engine.begin() as connection:
                await connection.execute(DropSchema(self.schema, cascade=True))
            await self.admin_engine.dispose()
        self.tmp.cleanup()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()

    def data(self, **kwargs: object) -> RandomDiscountInput:
        return replace(RandomDiscountInput(
            title="تخفیف شانسی", mode="automatic", reduction_kind="amount", minimum=10,
            maximum=30, step=1, window_minutes=60, request_key=secrets.token_hex(16),
        ), **kwargs)

    async def policy(self, **kwargs: object) -> RandomDiscount:
        return await create_random_discount(self.session, OWNER, self.data(**kwargs))

    async def test_disabled_policy_preserves_existing_price(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual((order.amount, order.discount_amount, order.random_discount_amount), (1000, 0, 0))

    async def test_automatic_discount_uses_integer_range_and_free_amounts(self) -> None:
        await self.policy(minimum=10, maximum=12)
        orders = [await create_order(self.session, user_id=user, plan_id=1) for user in (1, 4, 5)]
        self.assertEqual({order.amount for order in orders}, {988, 989, 990})
        for order in orders:
            self.assertEqual(order.amount + order.discount_amount, 1000)
            self.assertEqual(order.random_discount_amount, order.discount_amount)
        self.assertIn("تخفیف تصادفی", await checkout_summary(self.session, orders[0], currency="تومان"))

    async def test_exhaustion_never_repeats_or_exceeds_discount_range(self) -> None:
        await self.policy(minimum=10, maximum=11)
        for user in (1, 4):
            await create_order(self.session, user_id=user, plan_id=1)
        with self.assertRaises(ValueError):
            await create_order(self.session, user_id=5, plan_id=1)
        await self.session.rollback()
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 2)

    async def test_percent_range_and_monetary_step_are_respected(self) -> None:
        await self.policy(reduction_kind="percent", minimum=10, maximum=12, step=5)
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertIn(order.random_discount_amount, (100, 105, 110, 115, 120))

    async def test_large_percent_interval_selects_free_boundary_amounts(self) -> None:
        self.plan.price = 2_000_000_000
        await self.session.commit()
        await self.policy(reduction_kind="percent", minimum=1, maximum=99)
        self.session.add_all([
            Order(user_id=4, amount=1_980_000_000, status="delivered"),
            Order(user_id=6, amount=20_000_000, status="delivered"),
        ])
        await self.session.commit()
        with patch("app.services.random_discounts.secrets.randbelow", return_value=0):
            first = await create_order(self.session, user_id=1, plan_id=1)
        with patch("app.services.random_discounts.secrets.randbelow", side_effect=lambda count: count - 1):
            last = await create_order(self.session, user_id=5, plan_id=1)
        self.assertEqual((first.amount, last.amount), (1_979_999_999, 20_000_001))

    async def test_existing_recent_order_amount_is_not_selected(self) -> None:
        await self.policy(minimum=10, maximum=11)
        self.session.add(Order(user_id=4, amount=990, status="delivered"))
        await self.session.commit()
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 989)

    async def test_old_outstanding_order_and_wallet_topup_remain_protected(self) -> None:
        await self.policy(minimum=10, maximum=12)
        old = datetime.now(timezone.utc) - timedelta(days=2)
        self.session.add_all([
            Order(user_id=4, amount=990, status="awaiting_receipt", created_at=old),
            Payment(user_id=5, amount=989, is_wallet_topup=True, status="pending", created_at=old),
        ])
        await self.session.commit()
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 988)

    async def test_recent_completion_and_receipt_protect_old_payment_amounts(self) -> None:
        await self.policy(minimum=10, maximum=12)
        old = datetime.now(timezone.utc) - timedelta(days=2)
        self.session.add_all([
            Order(user_id=4, amount=990, status="delivered", created_at=old),
            Payment(user_id=5, amount=989, is_wallet_topup=True, status="approved", created_at=old,
                    receipt_at=datetime.now(timezone.utc)),
        ])
        await self.session.commit()
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 988)

    async def test_parallel_customers_receive_distinct_amounts(self) -> None:
        await self.policy(minimum=10, maximum=20)

        async def purchase(user_id: int) -> int:
            async with self.Session() as session:
                order = await create_order(session, user_id=user_id, plan_id=1)
                return order.amount

        amounts = await asyncio.gather(*(purchase(i) for i in (1, 4, 5, 6, 7)))
        self.assertEqual(len(set(amounts)), 5)

    async def test_repeated_draw_and_payment_switch_preserve_snapshot(self) -> None:
        await self.policy()
        order = await create_order(self.session, user_id=1, plan_id=1)
        amount = order.amount
        self.assertTrue(await apply_random_discount(self.session, order))
        await self.session.commit()
        with patch("app.services.orders._require_checkout_contact", AsyncMock()):
            first = await start_method_payment(self.session, order, 1, "card")
            second = await start_method_payment(self.session, order, 1, "crypto")
        self.assertEqual((order.amount, first.amount, second.amount), (amount, amount, amount))
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 1)

    async def test_rejecting_receipt_does_not_restore_random_discount(self) -> None:
        await self.policy(mode="code", code="SURPRISE")
        order = await create_order(self.session, user_id=1, plan_id=1)
        await apply_discount_to_order(self.session, order, "SURPRISE")
        amount, discount = order.amount, order.discount_amount
        await _release_order_discount(self.session, order)
        await self.session.commit()
        self.assertEqual((order.amount, order.discount_amount), (amount, discount))

    async def test_cancel_and_recreate_cannot_reroll_same_price(self) -> None:
        await self.policy()
        order = await create_order(self.session, user_id=1, plan_id=1)
        await cancel_order(self.session, order)
        with self.assertRaises(ValueError):
            await create_order(self.session, user_id=1, plan_id=1)
        await self.session.rollback()
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 1)

    async def test_completed_purchase_allows_another_discounted_purchase(self) -> None:
        await self.policy()
        order = await create_order(self.session, user_id=1, plan_id=1)
        order.status = "delivered"
        await self.session.commit()
        second = await create_order(self.session, user_id=1, plan_id=1)
        self.assertNotEqual(order.amount, second.amount)

    async def test_code_reveals_only_persisted_result_and_repeats_safely(self) -> None:
        await self.policy(mode="code", code="SURPRISE")
        order = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 1000)
        await apply_discount_to_order(self.session, order, " surprise ")
        amount = order.amount
        await apply_discount_to_order(self.session, order, "SURPRISE")
        self.assertEqual(order.amount, amount)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 1)

    async def test_miniapp_code_checkout_uses_the_same_random_reservation(self) -> None:
        await self.policy(mode="code", code="SURPRISE")
        checkout = await start_discounted_checkout(
            self.session, user_id=1, shop_id=None, plan_id=1, expected_price=1000, code="SURPRISE",
        )
        self.assertEqual(checkout.original_amount, 1000)
        self.assertTrue(10 <= checkout.discount <= 30)

    async def test_explicit_ordinary_code_takes_precedence_at_creation(self) -> None:
        await self.policy()
        self.session.add(DiscountCode(code="REGULAR", percent=50))
        await self.session.commit()
        order = await create_order(self.session, user_id=1, plan_id=1, discount_code="REGULAR")
        self.assertEqual((order.amount, order.discount_amount, order.random_discount_amount), (500, 500, 0))

    async def test_miniapp_ordinary_code_takes_precedence_over_automatic_discount(self) -> None:
        await self.policy()
        self.session.add(DiscountCode(code="REGULAR", percent=50))
        await self.session.commit()
        checkout = await start_discounted_checkout(
            self.session, user_id=1, shop_id=None, plan_id=1, expected_price=1000, code="REGULAR",
        )
        self.assertEqual((checkout.amount, checkout.discount), (500, 500))
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 0)

    async def test_ordinary_code_cannot_change_an_existing_random_price(self) -> None:
        await self.policy()
        order = await create_order(self.session, user_id=1, plan_id=1)
        amount = order.amount
        self.session.add(DiscountCode(code="REGULAR", percent=50))
        await self.session.commit()
        with self.assertRaises(ValueError):
            await apply_discount_to_order(self.session, order, "REGULAR")
        await self.session.rollback()
        await self.session.refresh(order)
        self.assertEqual(order.amount, amount)

    async def test_shop_policy_never_changes_another_shop_order(self) -> None:
        await create_random_discount(self.session, SHOP, self.data(mode="code", code="SHOPLUCK"))
        order = await create_order(self.session, user_id=1, plan_id=1)
        with self.assertRaises(ValueError):
            await apply_discount_to_order(self.session, order, "SHOPLUCK")
        await self.session.rollback()
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 0)

    async def test_current_shop_must_match_loaded_order(self) -> None:
        await create_random_discount(self.session, SHOP, self.data())
        order = Order(user_id=1, reseller_id=2, plan_id=2, amount=1000, status="pending")
        self.session.add(order)
        await self.session.commit()
        with self.assertRaises(ValueError):
            await apply_random_discount(self.session, order)

    async def test_automatic_policy_preserves_free_and_trial_orders(self) -> None:
        await self.policy()
        self.plan.price = 0
        await self.session.commit()
        free = await create_order(self.session, user_id=1, plan_id=1)
        trial = await create_order(self.session, user_id=4, plan_id=4)
        self.assertEqual((free.amount, trial.amount), (0, 0))
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 0)

    async def test_paid_order_cannot_receive_new_random_code(self) -> None:
        await self.policy(mode="code", code="SURPRISE")
        order = Order(user_id=1, plan_id=1, amount=1000, status="paid")
        self.session.add(order)
        await self.session.commit()
        with self.assertRaises(ValueError):
            await apply_discount_to_order(self.session, order, "SURPRISE")

    async def test_stopping_policy_preserves_existing_order_amount(self) -> None:
        policy = await self.policy()
        order = await create_order(self.session, user_id=1, plan_id=1)
        amount = order.amount
        await stop_random_discount(self.session, OWNER, policy.id)
        self.assertTrue(await apply_random_discount(self.session, order))
        other = await create_order(self.session, user_id=4, plan_id=1)
        self.assertEqual((order.amount, other.amount), (amount, 1000))

    async def test_custom_and_wholesale_orders_receive_automatic_discount(self) -> None:
        await self.policy()
        settings = {
            "custom_plan_enabled": "1", "custom_plan_price_per_gb": "1000", "custom_plan_price_per_day": "1000",
            "custom_plan_group_ids": "1", "wholesale_enabled": "1", "wholesale_min_qty": "1",
            "wholesale_max_qty": "100", "wholesale_tiers": '[{"min":1,"percent":10}]',
        }
        with patch("app.services.users.get_all_settings", AsyncMock(return_value=settings)):
            custom = await create_custom_order(self.session, user_id=1, data_limit_gb=1, duration_days=1)
            wholesale = await create_wholesale_order(self.session, user_id=4, plan_id=1, quantity=2)
        self.assertTrue(10 <= custom.random_discount_amount <= 30)
        self.assertTrue(10 <= wholesale.random_discount_amount <= 30)
        self.assertEqual(custom.amount + custom.discount_amount, 2000)
        self.assertEqual(wholesale.amount + wholesale.discount_amount, 2000)
        summary = await checkout_summary(self.session, wholesale, currency="تومان")
        self.assertIn("تخفیف تصادفی", summary)
        self.assertIn(f"−{format_toman(wholesale.random_discount_amount)}", summary)
        self.assertIn(f"−{format_toman(200)}", summary)

    async def test_renewal_and_addon_request_retries_reuse_their_draw(self) -> None:
        await self.policy()
        renewal_key, addon_key = secrets.token_hex(16), secrets.token_hex(16)
        renewal = await renew_service_with_plan(
            self.session, user_id=1, service=self.service, plan=self.plan, request_key=renewal_key,
        )
        repeated = await renew_service_with_plan(
            self.session, user_id=1, service=self.service, plan=self.plan, request_key=renewal_key,
        )
        addon = await create_addon_order(
            self.session, user_id=1, service=self.service, pack=self.pack, request_key=addon_key,
        )
        repeated_addon = await create_addon_order(
            self.session, user_id=1, service=self.service, pack=self.pack, request_key=addon_key,
        )
        self.assertEqual((renewal.id, renewal.amount), (repeated.id, repeated.amount))
        self.assertEqual((addon.id, addon.amount), (repeated_addon.id, repeated_addon.amount))
        self.assertTrue(10 <= renewal.random_discount_amount <= 30)
        self.assertTrue(10 <= addon.random_discount_amount <= 30)

    async def test_invalid_numeric_configuration_never_creates_policy(self) -> None:
        for field in ("minimum", "maximum", "step", "window_minutes"):
            for value in (True, -1, 0, float("nan"), 1.5, "10"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    await create_random_discount(self.session, OWNER, self.data(**{field: value}))

    async def test_admin_request_is_idempotent_and_permission_is_required(self) -> None:
        data = self.data()
        first = await create_random_discount(self.session, OWNER, data)
        repeated = await create_random_discount(self.session, OWNER, data)
        self.assertEqual(first.id, repeated.id)
        with self.assertRaises(PermissionError):
            await create_random_discount(self.session, {"role": "reseller", "bot_user_id": 3, "permissions": []}, self.data())
