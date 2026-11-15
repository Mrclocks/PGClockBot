"""Renewing a reseller package: refills the admin's volume only, never touches customers, undoes a refusal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from app.db.models import PgAdminSubscription, WalletTransaction
from app.services.billing import GB
from app.services.pasarguard import PasarGuardError
from app.services.pg_admin_subscription import (
    NO_TIME_RENEWAL,
    RENEW_REFUSED,
    RENEW_UNKNOWN_WARNING,
)
from app.services.reseller_package import STALE_PAGE, package_overview, renewal_effect
from tests.test_reseller_package import BRONZE, SILVER, ResellerPackageTestCase, plan

UNTIMED = 7


class RenewalTestCase(ResellerPackageTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.session.add(plan(UNTIMED, "بدون زمان", users=20, gb=200, price=150_000, duration_days=0))
        await self.session.commit()
        self.pg.reset_admin_usage = AsyncMock(return_value={})
        self.pg.reset_user_by_id = AsyncMock()
        self.pg.get_users = AsyncMock(return_value={"users": [{"id": 1}, {"id": 2}]})

    async def sub(self) -> PgAdminSubscription:
        self.session.expire_all()
        return await self.session.scalar(select(PgAdminSubscription))

    async def refunds(self) -> int:
        return len(list(await self.session.scalars(
            select(WalletTransaction).where(WalletTransaction.user_id == 3, WalletTransaction.amount > 0)
        )))


class TimedRenewalTests(RenewalTestCase):
    async def test_renewal_refills_the_admin_and_never_touches_customers(self) -> None:
        before = await self.sub()
        old_expiry = before.expires_at
        result = await self.renew(BRONZE)
        self.pg.reset_admin_usage.assert_awaited_once_with("shop1")
        self.pg.reset_user_by_id.assert_not_awaited()
        self.pg.get_users.assert_not_awaited()
        self.assertTrue(result["usage_reset"] and result["timed"])
        self.assertEqual(result["warnings"], [])
        self.assertIn("دست‌نخورده", renewal_effect(result))
        sub = await self.sub()
        self.assertGreater(sub.expires_at, old_expiry or datetime.now(timezone.utc).replace(tzinfo=None))
        self.assertEqual(await self.wallet(), 900_000)

    async def test_expired_subscription_is_active_again_after_paying(self) -> None:
        sub = await self.sub()
        sub.access_status = "expired"
        sub.expires_at = datetime.now(timezone.utc) - timedelta(days=3)
        await self.session.commit()
        await self.renew(BRONZE)
        sub = await self.sub()
        self.assertEqual(sub.access_status, "active")
        expires = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
        self.assertGreater(expires, datetime.now(timezone.utc) + timedelta(days=29), "counted from now, not from the old date")

    async def test_panel_refusal_of_the_volume_refill_returns_the_money_and_the_old_period(self) -> None:
        before = await self.sub()
        snapshot = (before.expires_at, before.base_gb, before.base_users, before.plan_id)
        self.pg.reset_admin_usage = AsyncMock(side_effect=PasarGuardError("no permission", 403))
        with self.assertRaises(ValueError) as caught:
            await self.renew(SILVER)
        self.assertEqual(str(caught.exception), RENEW_REFUSED)
        after = await self.sub()
        self.assertEqual((after.expires_at, after.base_gb, after.base_users, after.plan_id), snapshot)
        self.assertEqual((await self.wallet(), await self.refunds()), (1_000_000, 1))
        self.pg.modify_admin.assert_not_awaited()
        with self.assertRaises(ValueError) as stale:
            await self.renew(SILVER)
        self.assertEqual(str(stale.exception), STALE_PAGE, "the screen the refused renewal came from is spent")
        self.assertEqual((await self.profile()).plan_id, BRONZE)

    async def test_panel_refusal_of_the_new_limits_returns_the_money(self) -> None:
        self.pg.modify_admin = AsyncMock(side_effect=PasarGuardError("bad limits", 422))
        with self.assertRaises(ValueError) as caught:
            await self.renew(SILVER)
        self.assertEqual(str(caught.exception), RENEW_REFUSED)
        sub = await self.sub()
        self.assertEqual((sub.base_gb, sub.base_users, sub.plan_id), (100, 10, BRONZE))
        self.assertEqual((await self.wallet(), (await self.profile()).plan_id), (1_000_000, BRONZE))

    async def test_a_lost_answer_keeps_the_renewal_and_warns(self) -> None:
        for step in ("reset_admin_usage", "modify_admin"):
            with self.subTest(step=step):
                setattr(self.pg, step, AsyncMock(side_effect=httpx.ReadTimeout("slow")))
                before = await self.wallet()
                generation = (await self.sub()).renew_generation
                result = await self.renew(BRONZE, generation=generation)
                self.assertIn(RENEW_UNKNOWN_WARNING, result["warnings"])
                self.assertEqual(await self.wallet(), before - 100_000, "no blind refund")

    async def test_the_panel_never_receives_a_per_customer_reset(self) -> None:
        await self.renew(SILVER)
        self.pg.reset_user_by_id.assert_not_awaited()


class UntimedPlanTests(RenewalTestCase):
    async def make_current_untimed(self) -> None:
        sub = await self.sub()
        sub.plan_id = UNTIMED
        sub.expires_at = None
        (await self.profile()).plan_id = UNTIMED
        await self.session.commit()

    async def test_a_plan_without_time_cannot_be_renewed_and_costs_nothing(self) -> None:
        await self.make_current_untimed()
        with self.assertRaises(ValueError) as caught:
            await self.renew(UNTIMED)
        self.assertEqual(str(caught.exception), NO_TIME_RENEWAL)
        self.assertEqual((await self.wallet(), await self.debits()), (1_000_000, 0))
        self.pg.reset_admin_usage.assert_not_awaited()
        self.pg.modify_admin.assert_not_awaited()

    async def test_the_offer_says_why_a_plan_without_time_has_no_renewal(self) -> None:
        await self.make_current_untimed()
        overview = await package_overview(self.session, await self.profile())
        current = next(c for c in overview.choices if c.relation == "current")
        self.assertEqual(current.blocked, NO_TIME_RENEWAL)

    async def test_moving_to_a_plan_without_time_applies_capacity_only(self) -> None:
        result = await self.renew(UNTIMED)
        self.assertTrue(result["changed"])
        sub = await self.sub()
        self.assertEqual((sub.plan_id, sub.base_gb, sub.base_users), (UNTIMED, 200, 20))
        payload = self.pg.modify_admin.await_args.args[1]
        self.assertEqual((payload["data_limit"], payload["max_users"]), (200 * GB, 20))
        self.pg.reset_admin_usage.assert_not_awaited()
        self.pg.reset_user_by_id.assert_not_awaited()
        self.assertFalse(result["timed"])
        self.assertEqual(await self.wallet(), 850_000)
        self.assertIn("زمان و مصرف تغییری نکرد", renewal_effect(result))
