"""Owner bot edits of a package's PasarGuard access and PAYG options: validation and screen."""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers.admin_resplan_payg import screen_keyboard, screen_text
from app.db.models import Base, ResellerPlan
from app.services.reseller_plan_payg import (
    PlanFlag,
    PlanPrice,
    parse_plan_price,
    set_plan_basis,
    set_plan_price,
    toggle_plan_flag,
)


class PlanEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                ResellerPlan(id=1, name="<i>payg</i>", billing_mode="payg", price_per_gb=1000, payg_basis="traffic"),
                ResellerPlan(id=2, name="fixed", billing_mode="fixed"),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_switching_to_user_billing_needs_a_user_price_first(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await set_plan_basis(session, 1, "user")
            self.assertEqual((await session.get(ResellerPlan, 1)).payg_basis, "traffic")  # refused change undone
            await set_plan_price(session, 1, PlanPrice.USER, 5_000)
            plan = await set_plan_basis(session, 1, "user")
        self.assertEqual((plan.payg_basis, plan.payg_user_price), ("user", 5_000))

    async def test_flags_toggle_and_payg_options_stay_on_payg_packages(self) -> None:
        async with self.Session() as session:
            self.assertFalse((await toggle_plan_flag(session, 2, PlanFlag.PG_ACCESS)).allow_pg_panel_access)
            self.assertTrue((await toggle_plan_flag(session, 1, PlanFlag.UPFRONT)).payg_charge_upfront)
            with self.assertRaises(ValueError):
                await toggle_plan_flag(session, 2, PlanFlag.UPFRONT)
            with self.assertRaises(ValueError):
                await set_plan_basis(session, 1, "evil")
            with self.assertRaises(ValueError):
                await toggle_plan_flag(session, 99, PlanFlag.PG_ACCESS)

    async def test_unlimited_price_can_be_forbidden_again(self) -> None:
        async with self.Session() as session:
            await set_plan_price(session, 1, PlanPrice.UNLIMITED, 90_000)
            plan = await set_plan_price(session, 1, PlanPrice.UNLIMITED, parse_plan_price("-", PlanPrice.UNLIMITED))
        self.assertIsNone(plan.payg_unlimited_price)

    def test_typed_prices_are_validated(self) -> None:
        self.assertEqual(parse_plan_price("۱۲۰۰۰", PlanPrice.USER), 12_000)
        for bad in ("-", "abc", "-5", "99999999999"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_plan_price(bad, PlanPrice.USER)

    async def test_screen_escapes_the_name_and_offers_payg_options_only_on_payg(self) -> None:
        async with self.Session() as session:
            payg, fixed = await session.get(ResellerPlan, 1), await session.get(ResellerPlan, 2)
        self.assertNotIn("<i>payg</i>", screen_text(payg))
        payg_data = [b.callback_data for row in screen_keyboard(payg).inline_keyboard for b in row]
        fixed_data = [b.callback_data for row in screen_keyboard(fixed).inline_keyboard for b in row]
        self.assertIn("adm:rpg:b:user_traffic:1", payg_data)
        self.assertEqual(fixed_data, ["adm:rpg:f:acc:2", "adm:resplan:view:2"])
        self.assertTrue(all(len(d.encode()) <= 64 for d in payg_data))


if __name__ == "__main__":
    unittest.main()
