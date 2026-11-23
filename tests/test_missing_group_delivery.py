"""A panel answer «Group not found» names the plan's group ids instead of a bare 404."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, Order, OrderStatus, Plan
from app.services.orders import deliver_order
from app.services.pasarguard import PasarGuardError, explain_missing_group
from tests.postgres_support import scoped_async_engine


class ExplainMissingGroupTests(unittest.TestCase):
    def test_group_404_names_the_plan_group_ids(self):
        exc = PasarGuardError("POST /api/user failed (404)", 404, {"detail": "Group not found"})
        out = explain_missing_group(exc, [3, 7])
        self.assertEqual(out.status_code, 404)
        self.assertIn("3, 7", str(out))

    def test_other_errors_are_returned_unchanged(self):
        not_group = PasarGuardError("failed (404)", 404, {"detail": "User not found"})
        conflict = PasarGuardError("failed (409)", 409, {"detail": "Group already exists"})
        self.assertIs(explain_missing_group(not_group, [1]), not_group)
        self.assertIs(explain_missing_group(conflict, [1]), conflict)


class GroupPlanDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        buyer = BotUser(telegram_id=92001, referral_code="grp-buyer")
        plan = Plan(name="Monthly", price=100, pg_group_ids="4,9")
        self.session.add_all([buyer, plan])
        await self.session.flush()
        self.order = Order(
            id=21, user_id=buyer.id, plan_id=plan.id, amount=100, status=OrderStatus.PAID.value
        )
        self.session.add(self.order)
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_missing_group_error_lists_ids_and_keeps_status(self):
        pg = SimpleNamespace(
            create_user=AsyncMock(
                side_effect=PasarGuardError("POST /api/user failed (404)", 404, {"detail": "Group not found"})
            )
        )
        with (
            patch("app.services.orders.get_pg", return_value=pg),
            patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
        ):
            with self.assertRaises(PasarGuardError) as ctx:
                await deliver_order(self.session, self.order)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("4, 9", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
