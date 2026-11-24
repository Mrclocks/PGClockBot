"""Sale plans whose PasarGuard groups the panel assigns to a reseller's users."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, Plan, ResellerProfile, UserService
from app.services import plan_panel_groups as panel_groups
from app.services.orders import deliver_order
from app.services.plans_catalog import ROLE_REQUIRES_TEMPLATE_MESSAGE
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
LOAD_OPTIONS = "app.services.plan_panel_groups.load_pg_plan_options"


def shop_staff(**access) -> dict:
    """A reseller signed in with its own PasarGuard admin; ``access`` is the role's access block."""
    return {"role": "reseller", "bot_user_id": 5, "pg_access": access}


OWNER = {"role": "admin", "pg_is_owner": True}


class PanelGroupsOfferedTests(unittest.TestCase):
    def test_offered_when_the_role_was_read_and_shows_no_group(self) -> None:
        self.assertTrue(panel_groups.panel_groups_offered(shop_staff(), visible_groups=[], pg_error=None))

    def test_not_offered_when_the_role_shows_groups(self) -> None:
        offered = panel_groups.panel_groups_offered(shop_staff(), visible_groups=[{"id": 1}], pg_error=None)
        self.assertFalse(offered)

    def test_not_offered_when_the_read_failed(self) -> None:
        offered = panel_groups.panel_groups_offered(shop_staff(), visible_groups=[], pg_error="پنل در دسترس نیست")
        self.assertFalse(offered)

    def test_not_offered_to_a_template_only_role(self) -> None:
        staff = shop_staff(require_template=True)
        self.assertFalse(panel_groups.panel_groups_offered(staff, visible_groups=[], pg_error=None))

    def test_not_offered_outside_a_shop(self) -> None:
        for staff in (None, OWNER, {"role": "pg_staff"}):
            with self.subTest(staff=staff):
                self.assertFalse(panel_groups.panel_groups_offered(staff, visible_groups=[], pg_error=None))


class ResolvePlanGroupTargetTests(unittest.IsolatedAsyncioTestCase):
    async def resolve(self, staff: dict | None, selected: list[int], *, groups=(), pg_error=None):
        loader = AsyncMock(return_value=([], list(groups), pg_error))
        with patch(LOAD_OPTIONS, loader):
            target = await panel_groups.resolve_plan_group_target(MagicMock(), staff, selected)
        return target, loader

    async def test_chosen_groups_are_stored_without_a_panel_read(self) -> None:
        target, loader = await self.resolve(shop_staff(allowed_group_ids=[1, 2]), [1, 2])
        self.assertEqual(target, panel_groups.PlanGroupTarget(group_csv="1,2", panel_groups=False))
        loader.assert_not_awaited()

    async def test_a_group_outside_the_role_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.resolve(shop_staff(allowed_group_ids=[1]), [1, 3])

    async def test_no_group_falls_back_to_panel_groups_when_the_role_shows_none(self) -> None:
        target, loader = await self.resolve(shop_staff(allowed_group_ids=[]), [])
        self.assertEqual(target, panel_groups.PlanGroupTarget(group_csv=None, panel_groups=True))
        loader.assert_awaited_once()

    async def test_no_group_is_refused_when_the_role_shows_groups(self) -> None:
        with self.assertRaisesRegex(ValueError, panel_groups.PICK_A_GROUP_MESSAGE):
            await self.resolve(shop_staff(allowed_group_ids=[1]), [], groups=[{"id": 1}])

    async def test_no_group_is_refused_while_the_panel_is_unreachable(self) -> None:
        with self.assertRaisesRegex(ValueError, "پنل در دسترس نیست"):
            await self.resolve(shop_staff(allowed_group_ids=[]), [], pg_error="پنل در دسترس نیست")

    async def test_no_group_is_refused_for_the_platform_owner_without_a_panel_read(self) -> None:
        loader = AsyncMock(return_value=([], [], None))
        with patch(LOAD_OPTIONS, loader), self.assertRaisesRegex(ValueError, panel_groups.PICK_A_GROUP_MESSAGE):
            await panel_groups.resolve_plan_group_target(MagicMock(), OWNER, [])
        loader.assert_not_awaited()

    async def test_template_only_role_cannot_pick_groups_either_way(self) -> None:
        staff = shop_staff(require_template=True, allowed_group_ids=[1])
        with self.assertRaisesRegex(ValueError, ROLE_REQUIRES_TEMPLATE_MESSAGE):
            await self.resolve(staff, [1])
        with self.assertRaisesRegex(ValueError, panel_groups.PICK_A_GROUP_MESSAGE):
            await self.resolve(staff, [])


class PlanLinkTests(unittest.TestCase):
    def test_panel_groups_count_as_a_link_only_without_a_template(self) -> None:
        self.assertTrue(panel_groups.plan_has_pg_link(Plan(pg_panel_groups=True)))
        self.assertFalse(panel_groups.plan_has_pg_link(Plan(pg_panel_groups=False)))
        self.assertFalse(panel_groups.plan_uses_panel_groups(Plan(pg_panel_groups=True, pg_template_id=4)))

    def test_a_user_without_any_group_is_refused(self) -> None:
        panel_groups.assert_panel_assigned_group({"id": 1, "group_ids": [3]})
        panel_groups.assert_panel_assigned_group({"id": 1, "groups": [{"id": 3}]})
        for user in ({"id": 1, "group_ids": []}, {"id": 1}, None):
            with self.subTest(user=user), self.assertRaises(ValueError):
                panel_groups.assert_panel_assigned_group(user)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.buyer = BotUser(telegram_id=9001, username="buyer", referral_code="b1")
        self.plan = Plan(name="Monthly", price=100, duration_days=30, data_limit_gb=30, pg_panel_groups=True)
        self.session.add_all([self.buyer, self.plan])
        await self.session.commit()
        patcher = patch("app.services.users.get_all_settings", AsyncMock(return_value={}))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def paid_order(self) -> Order:
        order = Order(user_id=self.buyer.id, plan_id=self.plan.id, amount=100, status=OrderStatus.PAID.value)
        self.session.add(order)
        await self.session.commit()
        return order

    async def test_delivery_leaves_the_groups_to_the_panel(self) -> None:
        order = await self.paid_order()
        created = {"id": 7, "username": "clk_1", "group_ids": [3], "subscription_url": "https://x/sub/t"}
        pg = SimpleNamespace(create_user=AsyncMock(return_value=created))
        with patch("app.services.orders.get_pg", return_value=pg):
            delivered = await deliver_order(self.session, order)

        self.assertEqual(delivered.status, OrderStatus.DELIVERED.value)
        self.assertNotIn("group_ids", pg.create_user.call_args.args[0])
        service = await self.session.get(UserService, delivered.service_id)
        self.assertEqual(service.pg_user_id, 7)

    async def test_a_user_the_panel_left_without_groups_is_removed_and_not_delivered(self) -> None:
        order = await self.paid_order()
        pg = SimpleNamespace(
            create_user=AsyncMock(return_value={"id": 8, "username": "clk_1", "group_ids": []}),
            delete_user_by_id=AsyncMock(),
        )
        with patch("app.services.orders.get_pg", return_value=pg):
            with self.assertRaisesRegex(ValueError, "هیچ گروهی"):
                await deliver_order(self.session, order)

        pg.delete_user_by_id.assert_awaited_once_with(8)
        await self.session.refresh(order)
        self.assertNotEqual(order.status, OrderStatus.DELIVERED.value)
        self.assertIsNone(order.service_id)
        count = await self.session.scalar(select(func.count()).select_from(UserService))
        self.assertEqual(count, 0)

    async def test_a_plan_without_groups_or_panel_groups_still_refuses_delivery(self) -> None:
        self.plan.pg_panel_groups = False
        await self.session.commit()
        order = await self.paid_order()
        pg = SimpleNamespace(create_user=AsyncMock())
        with patch("app.services.orders.get_pg", return_value=pg):
            with self.assertRaises(ValueError):
                await deliver_order(self.session, order)
        pg.create_user.assert_not_awaited()


class ResellerBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.owner = BotUser(telegram_id=9100, username="shop", referral_code="s1", role="reseller")
        self.session.add(self.owner)
        await self.session.flush()
        self.profile = ResellerProfile(user_id=self.owner.id, is_active=True, bot_permissions="plans")
        self.session.add(self.profile)
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    def state(self) -> MagicMock:
        data = {"name": "پلن", "price": 1000, "days": 30, "gb": 10}
        return MagicMock(get_data=AsyncMock(return_value=data), clear=AsyncMock())

    async def save(self, staff: dict, *, groups=()) -> MagicMock:
        from app.bot.handlers import reseller_plans as handler

        callback = MagicMock(answer=AsyncMock(), message=None)
        with (
            patch.object(handler, "_actor", AsyncMock(return_value=(self.owner.id, self.profile))),
            patch.object(handler, "_staff_ctx", AsyncMock(return_value=staff)),
            patch(LOAD_OPTIONS, AsyncMock(return_value=([], list(groups), None))),
        ):
            await handler.res_plan_save_panel_groups(callback, self.state(), self.session, self.owner)
        return callback

    async def plans(self) -> list[Plan]:
        return list((await self.session.scalars(select(Plan))).all())

    async def test_a_reseller_without_groups_saves_a_panel_groups_plan(self) -> None:
        staff = shop_staff(allowed_group_ids=[])
        staff["bot_user_id"] = self.owner.id
        await self.save(staff)
        [plan] = await self.plans()
        self.assertTrue(plan.pg_panel_groups)
        self.assertIsNone(plan.pg_group_ids)
        self.assertEqual(plan.owner_reseller_id, self.owner.id)

    async def test_a_reseller_who_sees_groups_cannot_skip_choosing(self) -> None:
        staff = shop_staff(allowed_group_ids=[1])
        staff["bot_user_id"] = self.owner.id
        callback = await self.save(staff, groups=[{"id": 1}])
        self.assertEqual(await self.plans(), [])
        callback.answer.assert_awaited_once_with(panel_groups.PICK_A_GROUP_MESSAGE, show_alert=True)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keeps_plans(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = ROOT / "alembic/versions/0131_plan_panel_groups.py"
        spec = importlib.util.spec_from_file_location("plan_panel_groups_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE plans (id INTEGER PRIMARY KEY, name VARCHAR(128))"))
            conn.execute(text("INSERT INTO plans VALUES (1, 'old')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIs(conn.execute(text("SELECT pg_panel_groups FROM plans")).scalar(), False)
                migration.downgrade()
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT name FROM plans")).scalar(), "old")
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
