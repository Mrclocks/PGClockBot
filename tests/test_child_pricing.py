"""Level-1 pricing of level-2 shops: two-step charges, reserved credit, strict parent scope."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    ChildPricePlan,
    OrgPrincipal,
    ResellerCharge,
    ResellerPlan,
    ResellerProfile,
    ShopCreditAccount,
)
from app.services.billing import shop_bills_usage
from app.services.child_pricing import (
    ChildPlanInput,
    ChildPricingError,
    assign_child_plan,
    create_child_plan,
    delete_child_plan,
    parent_scope,
    set_child_credit,
    update_child_plan,
)
from app.services.payg_charges import PARENT_SHORTFALL_MESSAGE, ChargeRequest, list_review_charges, refund_charge, reserve_charge, send_to_review, settle_charge
from app.services.credit_statements import LadderStep
from app.services.payg_gate import PARENT_OVERDUE_MESSAGE, assert_shop_can_afford, charge_shop_change
from app.services.payg_pricing import GB, ChargeEvent, ServiceChange
from app.services.provision_gate import ProvisionError
from app.services.shop_credit import CreditLimitChange, child_shop_ids, set_credit_limit, shop_credit_limit

ROOT = Path(__file__).resolve().parents[1]
PAGE = "/resellers/children-pricing"
L1, L2, OTHER_L1, OTHER_L2 = 5, 6, 8, 9


def plan_input(**kw) -> ChildPlanInput:
    base = dict(name="فروش", basis="traffic", price_per_gb=1500, user_price=0, user_price_per_30d=False, unlimited_price=None)
    base.update(kw)
    return ChildPlanInput(**base)


def request(gb: int, key: str) -> ChargeRequest:
    return ChargeRequest(request_key=key, change=ServiceChange(ChargeEvent.CREATE, data_limit_bytes=gb * GB, days=30),
                         pg_username="c1")


class CascadeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(ResellerPlan(id=1, name="owner payg", billing_mode="payg", price_per_gb=1000,
                                     payg_basis="traffic", payg_charge_upfront=True))
            for uid in (L1, L2, OTHER_L1, OTHER_L2):
                session.add(BotUser(id=uid, telegram_id=uid, referral_code=f"r{uid}", role="reseller", wallet_balance=50_000))
            session.add_all([
                ResellerProfile(id=1, user_id=L1, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerProfile(id=2, user_id=L2, billing_mode="fixed", payg_wallet_linked=True),
                ResellerProfile(id=3, user_id=OTHER_L1, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerProfile(id=4, user_id=OTHER_L2, billing_mode="fixed", payg_wallet_linked=True),
                OrgPrincipal(id=11, depth=1, status="active", reseller_profile_id=1),
                OrgPrincipal(id=12, depth=2, parent_id=11, status="active", reseller_profile_id=2),
                OrgPrincipal(id=13, depth=1, status="active", reseller_profile_id=3),
                OrgPrincipal(id=14, depth=2, parent_id=13, status="active", reseller_profile_id=4),
            ])
            await session.commit()
            self.plan = await create_child_plan(session, L1, plan_input())
            await assign_child_plan(session, L1, L2, self.plan.id)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def wallets(self) -> tuple[int, int]:
        async with self.Session() as session:
            return (int((await session.get(BotUser, L1)).wallet_balance), int((await session.get(BotUser, L2)).wallet_balance))

    async def reserve(self, gb: int, key: str):
        async with self.Session() as session:
            return await reserve_charge(session, await session.get(ResellerProfile, 2), request(gb, key))


class ScopeTests(CascadeCase):
    async def test_parents_see_and_change_only_their_own_children(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await child_shop_ids(session, L1), [L2])
            self.assertEqual(await child_shop_ids(session, OTHER_L1), [OTHER_L2])
            with self.assertRaises(ChildPricingError):
                await assign_child_plan(session, L1, OTHER_L2, self.plan.id)
            with self.assertRaises(ChildPricingError):
                await assign_child_plan(session, OTHER_L1, OTHER_L2, self.plan.id)  # another parent's plan
            with self.assertRaises(ChildPricingError):
                await set_child_credit(session, OTHER_L1, L2, 1_000, actor="x")

    async def test_a_plan_that_bills_nothing_is_refused(self) -> None:
        async with self.Session() as session:
            for bad in (plan_input(price_per_gb=0), plan_input(name=" "), plan_input(basis="user", user_price=0)):
                with self.subTest(bad=bad), self.assertRaises(ChildPricingError):
                    await create_child_plan(session, L1, bad)

    def test_only_level_one_staff_manage_children(self) -> None:
        with self.assertRaises(PermissionError):
            parent_scope({"role": "admin", "web_owner": True})
        with self.assertRaises(PermissionError):
            parent_scope({"role": "principal", "org_depth": 2, "bot_user_id": L2})


class PlanEditTests(CascadeCase):
    async def test_an_edit_prices_only_later_charges(self) -> None:
        first = await self.reserve(10, "order:7101")
        async with self.Session() as session:
            await update_child_plan(session, L1, self.plan.id, plan_input(name="تازه", price_per_gb=2000))
            with self.assertRaises(ChildPricingError):
                await update_child_plan(session, OTHER_L1, self.plan.id, plan_input(price_per_gb=1))
            with self.assertRaises(ChildPricingError):
                await update_child_plan(session, L1, self.plan.id, plan_input(price_per_gb=0))
        second = await self.reserve(10, "order:7102")
        self.assertEqual((int(first.amount), int(second.amount)), (15_000, 20_000))

    async def test_a_plan_in_use_or_of_another_parent_is_not_deleted(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ChildPricingError):
                await delete_child_plan(session, L1, self.plan.id)  # still assigned to L2
            with self.assertRaises(ChildPricingError):
                await delete_child_plan(session, OTHER_L1, self.plan.id)
            await assign_child_plan(session, L1, L2, None)
            await delete_child_plan(session, L1, self.plan.id)
            self.assertIsNone(await session.get(ChildPricePlan, self.plan.id))

    async def test_a_lost_price_list_refuses_the_sale_instead_of_making_it_free(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 2)
            profile.child_plan_id = 999
            await session.commit()
            with self.assertRaises(ProvisionError):
                await charge_shop_change(session, shop_id=L2, change=ServiceChange(ChargeEvent.CREATE, data_limit_bytes=GB, days=30),
                                         request_key="order:7103", pg_username="c1")
        self.assertEqual(await self.wallets(), (50_000, 50_000))


class ChargeTests(CascadeCase):
    async def test_two_step_charge_moves_both_wallets_and_refunds_both(self) -> None:
        charge = await self.reserve(10, "order:7001")
        self.assertEqual(await self.wallets(), (40_000, 35_000))  # parent pays the owner, child pays the parent
        self.assertIsNotNone(charge.linked_charge_id)
        async with self.Session() as session:
            self.assertTrue(await refund_charge(session, charge, reason="refused"))
            self.assertFalse(await refund_charge(session, charge, reason="refused"))
        self.assertEqual(await self.wallets(), (50_000, 50_000))

    async def test_settle_and_review_move_the_parent_half_too(self) -> None:
        charge = await self.reserve(5, "order:7002")
        async with self.Session() as session:
            await send_to_review(session, charge)
            listed = await list_review_charges(session, shop_id=None)
            self.assertEqual([c.id for c in listed], [charge.id])  # the parent half is not listed alone
            await settle_charge(session, charge)
            parent = await session.get(ResellerCharge, int(charge.linked_charge_id))
        self.assertEqual(parent.status, "settled")

    async def test_a_parent_shortfall_charges_nobody(self) -> None:
        async with self.Session() as session:
            (await session.get(BotUser, L1)).wallet_balance = 1_000
            await session.commit()
        with self.assertRaises(ValueError):
            await self.reserve(10, "order:7003")
        self.assertEqual(await self.wallets(), (1_000, 50_000))

    async def test_a_parent_shortfall_alerts_the_parent_not_the_child(self) -> None:
        async with self.Session() as session:
            (await session.get(BotUser, L1)).wallet_balance = 1_000
            await session.commit()
        change = ServiceChange(ChargeEvent.CREATE, data_limit_bytes=10 * GB, days=30)
        with patch("app.services.payg_gate.announce_wallet_short", new=AsyncMock()) as announce:
            async with self.Session() as session:
                with self.assertRaises(ProvisionError) as refused:
                    await charge_shop_change(session, shop_id=L2, change=change, request_key="order:7004", pg_username="c1")
                self.assertEqual(refused.exception.message, PARENT_SHORTFALL_MESSAGE)
                with self.assertRaises(ProvisionError):
                    await assert_shop_can_afford(session, L2, change)
        self.assertEqual([int(c.args[1].user_id) for c in announce.await_args_list], [L1, L1])
        self.assertEqual(await self.wallets(), (1_000, 50_000))

    async def test_an_overdue_parent_stops_its_childrens_sales(self) -> None:
        async with self.Session() as session:
            session.add(ShopCreditAccount(shop_id=L1, credit_limit=0, restriction=int(LadderStep.NO_CREATE)))
            await session.commit()
        change = ServiceChange(ChargeEvent.CREATE, data_limit_bytes=GB, days=30)
        async with self.Session() as session:
            with self.assertRaises(ProvisionError) as refused:
                await charge_shop_change(session, shop_id=L2, change=change, request_key="order:7005", pg_username="c1")
            self.assertEqual(refused.exception.message, PARENT_OVERDUE_MESSAGE)
            with self.assertRaises(ProvisionError):
                await assert_shop_can_afford(session, L2, change)
            # Already in the panel (reconciliation): still charged, like the shop's own ladder.
            self.assertIsNotNone(await charge_shop_change(session, shop_id=L2, change=change, request_key="recon:1:new:0",
                                                          pg_username="c1", ladder_exempt=True))
        self.assertEqual(await self.wallets(), (49_000, 48_500))

    async def test_child_shops_are_never_billed_for_usage(self) -> None:
        async with self.Session() as session:
            self.assertFalse(await shop_bills_usage(session, await session.get(ResellerProfile, 2)))


class CreditTests(CascadeCase):
    async def test_credit_handed_to_a_child_comes_out_of_the_parents_line(self) -> None:
        async with self.Session() as session:
            await set_credit_limit(session, L1, CreditLimitChange(limit=30_000), actor="owner")
            await set_child_credit(session, L1, L2, 20_000, actor="l1")
            self.assertEqual(await shop_credit_limit(session, L1), 10_000)
            self.assertEqual(await shop_credit_limit(session, L2), 20_000)
            with self.assertRaises(ChildPricingError):
                await set_child_credit(session, L1, L2, 30_001, actor="l1")
            await set_child_credit(session, L1, L2, 30_000, actor="l1")  # its own share is not double counted
            self.assertEqual(await shop_credit_limit(session, L1), 0)


class TemplateTests(unittest.TestCase):
    def test_plan_edit_and_delete_forms_are_escaped(self) -> None:
        from types import SimpleNamespace

        from jinja2 import ChainableUndefined

        from app.api.app import templates

        env = templates.env.overlay(undefined=ChainableUndefined)
        plan = SimpleNamespace(id=4, name="<b>x</b>", payg_basis="user", price_per_gb=0, payg_user_price=900,
                               payg_user_price_per_30d=True, payg_unlimited_price=None)
        html = env.get_template("children_pricing.html").render(
            request=SimpleNamespace(query_params={}, url=SimpleNamespace(path=PAGE), cookies={}),
            staff={"role": "reseller"}, plans=[plan], children=[], free_credit=0, csrf_token="t",
        )
        self.assertNotIn("<b>x</b>", html)
        self.assertIn(f'action="{PAGE}/plans/4"', html)
        self.assertIn(f'action="{PAGE}/plans/4/delete"', html)
        self.assertIn('value="user" selected', html)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        path = ROOT / "alembic/versions/0102_child_pricing.py"
        spec = importlib.util.spec_from_file_location("child_pricing_migration", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE reseller_profiles (id INTEGER PRIMARY KEY, user_id INTEGER)"))
            conn.execute(sa.text("CREATE TABLE reseller_charges (id INTEGER PRIMARY KEY, amount INTEGER)"))
            conn.execute(sa.text("INSERT INTO reseller_profiles (id, user_id) VALUES (1, 5)"))
            for step in (module.upgrade, module.upgrade):
                with Operations.context(MigrationContext.configure(conn)):
                    step()
            self.assertIn("child_plan_id", {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")})
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
            self.assertFalse(sa.inspect(conn).has_table("child_price_plans"))
            self.assertEqual(conn.execute(sa.text("SELECT user_id FROM reseller_profiles")).scalar(), 5)


if __name__ == "__main__":
    unittest.main()
