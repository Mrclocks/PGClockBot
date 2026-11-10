"""Telegram buyer and plan-volume variables in service naming."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, Plan, UserService
from app.services.orders import generate_pg_username


class NamingContextTests(unittest.IsolatedAsyncioTestCase):
    async def name(self, volume=30, username="ali_user", telegram_id=9001, **kwargs):
        plan = SimpleNamespace(
            data_limit_gb=volume, pg_username_prefix="vip", pg_username_suffix=None,
            pg_username_pattern="{prefix}_{username}_{plan_volume}{plan_unit}_{random}",
        )
        user = SimpleNamespace(username=username, telegram_id=telegram_id)
        with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
            "app.services.orders._random_alnum", return_value="a1b2c3d4"
        ):
            return await generate_pg_username(AsyncMock(), user_id=7, plan=plan, user=user, **kwargs)

    async def test_numeric_volume_and_unit_are_separate(self):
        self.assertEqual(await self.name(), "vip_ali_user_30GB_a1b2c3d4")

    async def test_fractional_volume(self):
        self.assertEqual(await self.name(volume=1.5), "vip_ali_user_1_5GB_a1b2c3d4")

    async def pattern_name(self, pattern, **plan_fields):
        plan = SimpleNamespace(data_limit_gb=30, pg_username_prefix=None, pg_username_suffix=None, pg_username_pattern=pattern, **plan_fields)
        user = SimpleNamespace(username="ali_user", telegram_id=9001)
        with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
            "app.services.orders._random_alnum", return_value="a1b2"
        ):
            return await generate_pg_username(AsyncMock(), user_id=7, plan=plan, user=user)

    async def test_username_only_pattern_stays_unique_per_purchase(self):
        self.assertEqual(await self.pattern_name("{username}"), "ali_user_a1b2")

    async def test_short_username_is_padded_to_panel_minimum(self):
        plan = SimpleNamespace(data_limit_gb=30, pg_username_prefix=None, pg_username_suffix=None, pg_username_pattern="{username}")
        user = SimpleNamespace(username="a", telegram_id=9001)
        with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
            "app.services.orders._random_alnum", return_value="a1b2"
        ):
            name = await generate_pg_username(AsyncMock(), user_id=7, plan=plan, user=user)
        self.assertRegex(name, r"^[A-Za-z0-9_]{3,32}$")

    async def test_pattern_with_dash_and_long_result_fit_panel_format(self):
        name = await self.pattern_name("{prefix}-{username}-{random}-" + "x" * 40)
        self.assertRegex(name, r"^[A-Za-z0-9_]{3,32}$")

    async def test_unlimited_and_zero(self):
        for volume in (None, 0):
            self.assertEqual(await self.name(volume=volume, username="ali"), "vip_ali_unlimitedGB_a1b2c3d4")

    async def test_user_without_username_uses_telegram_id(self):
        self.assertEqual(await self.name(username=None), "vip_9001_30GB_a1b2c3d4")

    async def test_at_sign_is_removed(self):
        self.assertEqual(await self.name(username="@ali_user"), "vip_ali_user_30GB_a1b2c3d4")

    async def test_global_pattern_receives_context_and_shop_scope(self):
        settings = AsyncMock(return_value={"pg_username_pattern": "{username}_{plan_volume}{plan_unit}_{random}"})
        session = AsyncMock()
        session.get.return_value = SimpleNamespace(username="shop_buyer", telegram_id=9001)
        with patch("app.services.users.get_all_settings", settings), patch(
            "app.services.orders._random_alnum", return_value="a1b2c3d4"
        ):
            name = await generate_pg_username(session, user_id=7, reseller_id=42, plan=SimpleNamespace(data_limit_gb=12))
        self.assertEqual(name, "shop_buyer_12GB_a1b2c3d4")
        settings.assert_awaited_once_with(session, reseller_id=42)
        session.get.assert_awaited_once_with(BotUser, 7)

    async def test_buyer_id_lookup_ignores_naming_id(self):
        """Delivery passes order.id as user_id; BotUser must be loaded by buyer_id."""
        settings = AsyncMock(
            return_value={"pg_username_pattern": "{id}_{username}_{random}"}
        )
        session = AsyncMock()
        session.get.return_value = SimpleNamespace(username="real_buyer", telegram_id=9001)
        with patch("app.services.users.get_all_settings", settings), patch(
            "app.services.orders._random_alnum", return_value="a1b2c3d4"
        ):
            name = await generate_pg_username(
                session,
                user_id=999,  # order id — must NOT be used for BotUser.get
                buyer_id=7,
                plan=SimpleNamespace(data_limit_gb=10),
            )
        self.assertEqual(name, "999_real_buyer_a1b2c3d4")
        session.get.assert_awaited_once_with(BotUser, 7)

    async def test_unsafe_username_chars_are_stripped(self):
        self.assertEqual(
            await self.name(username="ali user!@#"),
            "vip_aliuser_30GB_a1b2c3d4",
        )

    async def test_legacy_pattern_does_not_query_user(self):
        session = AsyncMock()
        with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
            "app.services.orders._random_alnum", return_value="a1b2c3d4"
        ):
            self.assertEqual(await generate_pg_username(session, user_id=7), "clk_a1b2c3d4")
        session.get.assert_not_awaited()

    def test_new_variables_are_isolated_to_naming(self):
        from app.services.message_variables import DOMAIN_NAMING, DOMAIN_PAYMENT, render_message_template
        template = "{plan_volume}{plan_unit}_{username}_{unknown}"
        values = dict(plan_volume="30", plan_unit="GB", username="ali")
        self.assertEqual(render_message_template(template, domain=DOMAIN_NAMING, html=False, **values), "30GB_ali_{unknown}")
        self.assertEqual(render_message_template(template, domain=DOMAIN_PAYMENT, **values), template)


class DeliveryNamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.sessions()
        self.user = BotUser(telegram_id=9001, username="buyer", referral_code="buyer")
        self.platform = Plan(name="Monthly", price=100, pg_template_id=1, data_limit_gb=30)
        self.session.add_all([self.user, self.platform])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_deliver_order_sends_real_buyer_and_volume_to_pg(self):
        from app.services.orders import deliver_order
        self.platform.pg_username_pattern = "{username}_{plan_volume}{plan_unit}_{random}"
        order = Order(user_id=self.user.id, plan_id=self.platform.id, amount=100, status=OrderStatus.PAID.value)
        self.session.add(order)
        await self.session.commit()
        pg = SimpleNamespace(create_user_from_template=AsyncMock(return_value={"id": 123, "subscription_url": "https://example.com/sub/test"}))
        with patch("app.services.orders.get_pg", return_value=pg), patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch("app.services.orders._random_alnum", return_value="a1b2c3d4"):
            delivered = await deliver_order(self.session, order)
        self.assertEqual(delivered.status, OrderStatus.DELIVERED.value)
        self.assertEqual(pg.create_user_from_template.call_args.args[0]["username"], "buyer_30GB_a1b2c3d4")
        service = await self.session.get(UserService, delivered.service_id)
        self.assertEqual(service.pg_username, "buyer_30GB_a1b2c3d4")

    async def test_deliver_order_id_placeholder_is_order_not_buyer(self):
        """When order.id != buyer.id, {id} stays order id and {username} stays buyer."""
        from app.services.orders import deliver_order

        # Pad Order PK so it diverges from BotUser.id (both tables start at 1).
        for _ in range(3):
            self.session.add(
                Order(
                    user_id=self.user.id,
                    plan_id=self.platform.id,
                    amount=1,
                    status=OrderStatus.CANCELLED.value,
                )
            )
        await self.session.commit()

        self.platform.pg_username_pattern = "{id}_{username}_{random}"
        order = Order(
            user_id=self.user.id,
            plan_id=self.platform.id,
            amount=100,
            status=OrderStatus.PAID.value,
        )
        self.session.add(order)
        await self.session.commit()
        self.assertNotEqual(order.id, self.user.id)

        pg = SimpleNamespace(
            create_user_from_template=AsyncMock(
                return_value={"id": 456, "subscription_url": "https://example.com/sub/x"}
            )
        )
        with (
            patch("app.services.orders.get_pg", return_value=pg),
            patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
            patch("app.services.orders._random_alnum", return_value="a1b2c3d4"),
        ):
            delivered = await deliver_order(self.session, order)

        expected = f"{order.id}_buyer_a1b2c3d4"
        self.assertEqual(delivered.status, OrderStatus.DELIVERED.value)
        self.assertEqual(pg.create_user_from_template.call_args.args[0]["username"], expected)
