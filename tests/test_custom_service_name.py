"""Buyer-chosen service name: validation, availability, storage and delivery."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, Plan, ResellerProfile, UserService
from app.services import custom_service_name as naming
from app.services.orders import create_with_name_fallback, deliver_order, generate_pg_username
from app.services.pasarguard import PasarGuardError

ENABLED = {naming.SETTING_KEY: "1"}


class NameRulesTests(unittest.TestCase):
    def test_a_valid_name_is_lower_cased(self) -> None:
        self.assertEqual(naming.normalize_service_name("  @Ali_22 "), "ali_22")

    def test_invalid_names_are_refused(self) -> None:
        for raw in ("ab", "1ali", "علی", "ali-22", "ali 22", "a" * 21, "", None):
            with self.assertRaises(ValueError, msg=repr(raw)):
                naming.normalize_service_name(raw)

    def test_a_random_name_follows_the_rules(self) -> None:
        for _ in range(20):
            name = naming.random_service_name()
            self.assertEqual(naming.normalize_service_name(name), name)


class NamingPatternTests(unittest.IsolatedAsyncioTestCase):
    async def name(self, pattern: str | None, chosen: str | None) -> str:
        plan = SimpleNamespace(data_limit_gb=30, pg_username_prefix="vip", pg_username_suffix="_x",
                               pg_username_pattern=pattern)
        with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
            "app.services.orders._random_alnum", return_value="a1b2c3d4"
        ):
            return await generate_pg_username(AsyncMock(), user_id=7, plan=plan, chosen_name=chosen)

    async def test_the_chosen_name_takes_the_random_part(self) -> None:
        self.assertEqual(await self.name("{prefix}_{random}{suffix}", "ali"), "vip_ali_x")

    async def test_a_pattern_without_random_still_carries_the_name(self) -> None:
        self.assertEqual(await self.name("{prefix}_{id}", "ali"), "vip_ali_x")

    async def test_without_a_chosen_name_nothing_changes(self) -> None:
        self.assertEqual(await self.name("{prefix}_{id}", None), "vip_7")


class NameFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_taken_name_falls_back_to_a_generated_one(self) -> None:
        create = AsyncMock(side_effect=[PasarGuardError("User already exists", status_code=409), {"id": 1}])
        regenerate = AsyncMock(return_value="clk_a1b2c3d4")

        result = await create_with_name_fallback(create, "clk_ali", regenerate=regenerate)

        self.assertEqual(result, ({"id": 1}, "clk_a1b2c3d4"))
        self.assertEqual([call.args[0] for call in create.await_args_list], ["clk_ali", "clk_a1b2c3d4"])

    async def test_other_panel_errors_are_not_retried(self) -> None:
        create = AsyncMock(side_effect=PasarGuardError("bad group", status_code=400))
        with self.assertRaises(PasarGuardError):
            await create_with_name_fallback(create, "clk_ali", regenerate=AsyncMock())
        create.assert_awaited_once()

    async def test_a_generated_name_is_never_retried(self) -> None:
        create = AsyncMock(side_effect=PasarGuardError("User already exists", status_code=409))
        with self.assertRaises(PasarGuardError):
            await create_with_name_fallback(create, "clk_a1b2c3d4", regenerate=None)
        create.assert_awaited_once()


class NameFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.buyer = BotUser(telegram_id=9001, username="buyer", referral_code="b1")
        self.other = BotUser(telegram_id=9002, username="other", referral_code="b2")
        self.plan = Plan(name="Monthly", price=100, pg_template_id=1, data_limit_gb=30)
        self.session.add_all([self.buyer, self.other, self.plan])
        await self.session.commit()
        patcher = patch("app.services.users.get_all_settings", AsyncMock(return_value={}))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def order(self, user: BotUser, **fields) -> Order:
        order = Order(user_id=user.id, plan_id=self.plan.id, amount=100, **fields)
        self.session.add(order)
        await self.session.commit()
        return order


class NameStepSwitchTests(NameFixture):
    async def shop(self, permissions: str) -> int:
        owner = BotUser(telegram_id=9100, username="shop", referral_code="s1", role="reseller")
        self.session.add(owner)
        await self.session.flush()
        self.session.add(
            ResellerProfile(user_id=owner.id, is_active=True, web_permissions=permissions, bot_permissions=permissions)
        )
        await self.session.commit()
        return owner.id

    async def test_a_shop_order_follows_the_shop_permission(self) -> None:
        granted = await self.shop("dashboard,custom_service_name")
        order = await self.order(self.buyer, reseller_id=granted)
        self.assertTrue(await naming.offers_custom_name(self.session, order))
        self.assertTrue(await naming.needs_service_name(self.session, order))
        order.requested_username = "ali"
        self.assertFalse(await naming.needs_service_name(self.session, order))

    async def test_a_shop_without_the_permission_has_no_name_step(self) -> None:
        shop_id = await self.shop("dashboard")
        order = await self.order(self.buyer, reseller_id=shop_id)
        with patch("app.services.custom_service_name.get_all_settings", AsyncMock(return_value=ENABLED)):
            # The platform bot switch does not turn the step on inside a shop.
            self.assertFalse(await naming.offers_custom_name(self.session, order))

    async def test_a_platform_order_follows_the_platform_switch(self) -> None:
        order = await self.order(self.buyer)
        with patch("app.services.custom_service_name.get_all_settings", AsyncMock(return_value={})):
            self.assertFalse(await naming.offers_custom_name(self.session, order))
        with patch("app.services.custom_service_name.get_all_settings", AsyncMock(return_value=ENABLED)):
            self.assertTrue(await naming.offers_custom_name(self.session, order))

    async def test_only_single_new_unpaid_orders_get_the_step(self) -> None:
        with patch("app.services.custom_service_name.get_all_settings", AsyncMock(return_value=ENABLED)):
            for fields in (
                {"quantity": 3},
                {"note": "renew:5"},
                {"status": OrderStatus.PAID.value},
            ):
                order = await self.order(self.buyer, **fields)
                self.assertFalse(await naming.offers_custom_name(self.session, order), fields)


class StoredNameTests(NameFixture):
    async def test_the_name_is_stored_and_the_final_username_returned(self) -> None:
        order = await self.order(self.buyer)

        username = await naming.set_service_name(self.session, order, buyer_id=self.buyer.id, raw="Ali")

        self.assertEqual(username, "clk_ali")
        self.assertEqual(order.requested_username, "ali")

    async def test_a_username_already_in_use_gets_a_random_ending(self) -> None:
        self.session.add(UserService(bot_user_id=self.other.id, pg_username="CLK_ali"))
        await self.session.commit()
        order = await self.order(self.buyer)

        with patch("app.services.orders._random_alnum", return_value="x7k2"):
            username = await naming.set_service_name(self.session, order, buyer_id=self.buyer.id, raw="ali")

        self.assertEqual(username, "clk_ali_x7k2")
        self.assertEqual(order.requested_username, "ali_x7k2")

    async def test_a_similar_but_different_name_is_free(self) -> None:
        self.session.add(UserService(bot_user_id=self.other.id, pg_username="clk_ali"))
        await self.session.commit()
        order = await self.order(self.buyer)

        self.assertEqual(
            await naming.set_service_name(self.session, order, buyer_id=self.buyer.id, raw="mali"), "clk_mali"
        )

    async def test_a_name_held_by_another_open_order_gets_a_random_ending(self) -> None:
        await self.order(self.other, requested_username="ali")
        order = await self.order(self.buyer)

        await naming.set_service_name(self.session, order, buyer_id=self.buyer.id, raw="ali")

        self.assertRegex(order.requested_username, r"^ali_[a-z0-9]{4}$")

    async def test_a_long_taken_name_stays_within_the_limit(self) -> None:
        wanted = "a" * naming.MAX_LENGTH
        await self.order(self.other, requested_username=wanted)
        order = await self.order(self.buyer)

        await naming.set_service_name(self.session, order, buyer_id=self.buyer.id, raw=wanted)

        self.assertLessEqual(len(order.requested_username), naming.MAX_LENGTH)
        self.assertNotEqual(order.requested_username, wanted)

    async def test_someone_elses_or_a_paid_order_cannot_be_renamed(self) -> None:
        theirs = await self.order(self.other)
        paid = await self.order(self.buyer, status=OrderStatus.PAID.value)

        for order in (theirs, paid):
            with self.assertRaisesRegex(ValueError, naming.ORDER_LOCKED):
                await naming.set_service_name(self.session, order, buyer_id=self.buyer.id, raw="ali")

    async def test_delivery_creates_the_service_with_the_chosen_name(self) -> None:
        order = await self.order(self.buyer, status=OrderStatus.PAID.value, requested_username="ali")
        pg = SimpleNamespace(
            create_user_from_template=AsyncMock(return_value={"id": 5, "subscription_url": "https://x/sub/t"})
        )
        with patch("app.services.orders.get_pg", return_value=pg):
            delivered = await deliver_order(self.session, order)

        self.assertEqual(delivered.status, OrderStatus.DELIVERED.value)
        self.assertEqual(pg.create_user_from_template.call_args.args[0]["username"], "clk_ali")
        service = await self.session.get(UserService, delivered.service_id)
        self.assertEqual(service.pg_username, "clk_ali")

    async def test_delivery_still_succeeds_when_the_panel_already_holds_the_name(self) -> None:
        order = await self.order(self.buyer, status=OrderStatus.PAID.value, requested_username="ali")
        pg = SimpleNamespace(
            create_user_from_template=AsyncMock(
                side_effect=[
                    PasarGuardError("User already exists", status_code=409),
                    {"id": 6, "subscription_url": "https://x/sub/u"},
                ]
            )
        )
        with patch("app.services.orders.get_pg", return_value=pg), patch(
            "app.services.orders._random_alnum", return_value="x7k2"
        ):
            delivered = await deliver_order(self.session, order)

        self.assertEqual(delivered.status, OrderStatus.DELIVERED.value)
        service = await self.session.get(UserService, delivered.service_id)
        self.assertEqual(service.pg_username, "clk_ali_x7k2")


class NameStepHandlerTests(NameFixture):
    async def enter(self, order: Order, text: str):
        from unittest.mock import MagicMock

        from app.bot.handlers import custom_service_name as handler

        state = MagicMock(
            get_data=AsyncMock(return_value={handler.ORDER_KEY: order.id}),
            set_state=AsyncMock(),
            update_data=AsyncMock(),
            clear=AsyncMock(),
        )
        message = MagicMock(text=text, answer=AsyncMock())
        with (
            patch.object(handler, "is_customer_order_in_current_shop", return_value=True),
            patch.object(handler, "present_order_pay", AsyncMock()) as shown,
        ):
            await handler.service_name_entered(message, state, self.session, self.buyer)
        return message, state, shown

    async def test_typed_cancel_leaves_the_step_without_naming(self) -> None:
        from app.bot.handlers import custom_service_name as handler

        order = await self.order(self.buyer)
        with patch.object(handler, "restore_main_reply", AsyncMock()) as home:
            _message, state, shown = await self.enter(order, "❌ انصراف")

        shown.assert_not_awaited()
        state.clear.assert_awaited()
        home.assert_awaited_once()
        await self.session.refresh(order)
        self.assertIsNone(order.requested_username)

    async def test_the_random_button_names_the_order_and_opens_payment(self) -> None:
        from unittest.mock import MagicMock

        from app.bot.handlers import custom_service_name as handler

        order = await self.order(self.buyer)
        callback = MagicMock(data=f"pay:namernd:{order.id}", answer=AsyncMock(), message=MagicMock())
        state = MagicMock(set_state=AsyncMock())
        with (
            patch.object(handler, "is_customer_order_in_current_shop", return_value=True),
            patch("app.services.custom_service_name.get_all_settings", AsyncMock(return_value=ENABLED)),
            patch.object(handler, "present_order_pay", AsyncMock()) as shown,
        ):
            await handler.random_service_name_chosen(callback, state, self.session, self.buyer)

        shown.assert_awaited_once()
        await self.session.refresh(order)
        self.assertRegex(order.requested_username, r"^[a-z][a-z0-9]{7}$")

    async def test_the_payment_screen_asks_for_the_name_first(self) -> None:
        from unittest.mock import MagicMock

        from app.bot import menu_nav

        order = await self.order(self.buyer)
        state = MagicMock(set_state=AsyncMock(), update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
        with (
            patch.object(menu_nav, "get_all_settings", AsyncMock(return_value={})),
            patch("app.services.custom_service_name.get_all_settings", AsyncMock(return_value=ENABLED)),
            patch.object(menu_nav, "set_nav_level", AsyncMock()),
            patch("app.bot.nav_inline.present_inline_only", AsyncMock()) as shown,
        ):
            await menu_nav.present_order_pay(
                MagicMock(), self.session, self.buyer, order.id, state=state, summary="S",
                is_reseller_bot=False, reseller_owner_id=None,
            )
            markup = shown.await_args.kwargs["inline"]
            buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertEqual(buttons, [f"pay:namernd:{order.id}", f"pay:back:{order.id}"])

            order.requested_username = "ali"
            await self.session.commit()
            await menu_nav.present_order_pay(
                MagicMock(), self.session, self.buyer, order.id, state=state, summary="S",
                is_reseller_bot=False, reseller_owner_id=None,
            )
            buttons = [b.callback_data for row in shown.await_args.kwargs["inline"].inline_keyboard for b in row]
            self.assertNotIn(f"pay:namernd:{order.id}", buttons)

    async def test_an_invalid_name_keeps_the_step_open(self) -> None:
        message, state, shown = await self.enter(await self.order(self.buyer), "علی")

        shown.assert_not_awaited()
        state.set_state.assert_not_awaited()
        self.assertIn("حروف انگلیسی", message.answer.await_args.args[0])

    async def test_a_valid_name_returns_to_the_payment_screen(self) -> None:
        order = await self.order(self.buyer)

        _message, _state, shown = await self.enter(order, "ali")

        shown.assert_awaited_once()
        summary = shown.await_args.kwargs["summary"]
        self.assertEqual(summary.count("clk_ali"), 1)
        await self.session.refresh(order)
        self.assertEqual(order.requested_username, "ali")


class RequestedUsernameMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_orders(self) -> None:
        import importlib.util
        from pathlib import Path

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0085_order_requested_username.py"
        spec = importlib.util.spec_from_file_location("requested_username_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER)"))
            conn.execute(sa.text("INSERT INTO orders VALUES (1, 500)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(conn.execute(sa.text("SELECT requested_username FROM orders")).scalar(), None)
                migration.downgrade()
                columns = {column["name"] for column in sa.inspect(conn).get_columns("orders")}
                self.assertNotIn("requested_username", columns)
                self.assertEqual(conn.execute(sa.text("SELECT amount FROM orders")).scalar(), 500)
                migration.downgrade()
        engine.dispose()
