"""A service the panel confirmed gone cannot be renewed or extended, and retrying its paid order says why."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, DeliveryFailure, Order, UserService
from app.services import delivery_failures as ux20
from app.services.pasarguard import PasarGuardError
from app.services.service_addons import KIND_VOLUME, validate_addon_selection
from app.services.service_renewals import SERVICE_REMOVED_FROM_PANEL, validate_renewal_selection
from tests.postgres_support import scoped_async_engine

REMOVED_AT = datetime(2026, 11, 20, tzinfo=timezone.utc)


def service(**fields) -> SimpleNamespace:
    base = dict(id=7, bot_user_id=1, pg_user_id=713, remark="", panel_missing_at=REMOVED_AT, shop_id=None, plan_id=None)
    return SimpleNamespace(**{**base, **fields})


class PurchaseGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_renewal_of_a_service_removed_from_the_panel_is_refused_before_any_order(self) -> None:
        plan = SimpleNamespace(is_active=True, is_trial=False, owner_reseller_id=None)
        session = AsyncMock()
        with (
            patch("app.services.service_renewals.assert_not_frozen", AsyncMock()),
            self.assertRaisesRegex(ValueError, SERVICE_REMOVED_FROM_PANEL),
        ):
            await validate_renewal_selection(session, user_id=1, service=service(), plan=plan, shop_id=None)
        session.execute.assert_not_awaited()

    async def test_a_pack_for_a_service_removed_from_the_panel_is_refused(self) -> None:
        pack = SimpleNamespace(is_active=True, kind=KIND_VOLUME, owner_reseller_id=None)
        session = AsyncMock()
        session.get.return_value = SimpleNamespace(id=1)
        with (
            patch("app.services.service_addons.pack_matches_shop", return_value=True),
            self.assertRaisesRegex(ValueError, SERVICE_REMOVED_FROM_PANEL),
        ):
            await validate_addon_selection(session, user_id=1, service=service(), pack=pack, shop_id=None)

    async def test_a_service_still_on_the_panel_passes_the_guard(self) -> None:
        plan = SimpleNamespace(is_active=True, is_trial=False, owner_reseller_id=None)
        with (
            patch("app.services.service_renewals.assert_not_frozen", AsyncMock()),
            patch("app.services.service_automation.service_shop_id", AsyncMock(return_value=None)),
            patch("app.services.service_renewals.assert_service_quota_available", AsyncMock()),
            patch("app.services.service_renewals.renewal_terms", return_value={"ok": True}),
        ):
            terms = await validate_renewal_selection(
                AsyncMock(), user_id=1, service=service(panel_missing_at=None), plan=plan, shop_id=None
            )
        self.assertEqual(terms, {"ok": True})


class RetryMissingPanelUserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(BotUser(id=1, telegram_id=101, referral_code="u1", role="user"))
        await self.session.flush()
        self.session.add(UserService(id=7, bot_user_id=1, pg_user_id=713, pg_username="u713", panel_missing_at=REMOVED_AT))
        await self.session.flush()
        self.session.add(Order(id=652, user_id=1, amount=210000, status="paid", note="addon:1:7:volume:30", service_id=7))
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def retry(self, error: Exception) -> Exception:
        with patch("app.services.orders.fulfill_paid_order", AsyncMock(side_effect=error)):
            with self.assertRaises(Exception) as caught:
                await ux20.retry_delivery(self.session, 652)
        return caught.exception

    async def test_a_missing_panel_user_is_reported_as_not_retryable_and_recorded(self) -> None:
        error = await self.retry(PasarGuardError("GET /api/user/by-id/713 failed (404): User not found", 404))
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), ux20.SERVICE_USER_MISSING)
        failures = (await self.session.scalars(select(DeliveryFailure).where(DeliveryFailure.order_id == 652))).all()
        self.assertEqual(len(failures), 1)
        order = await self.session.get(Order, 652)
        self.assertEqual(order.status, "paid")

    async def test_a_panel_outage_is_passed_on_unchanged(self) -> None:
        error = await self.retry(PasarGuardError("bad gateway", 502))
        self.assertIsInstance(error, PasarGuardError)


if __name__ == "__main__":
    unittest.main()
