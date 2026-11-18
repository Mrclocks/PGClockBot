"""A renewal opens a period: display and low-volume alerts use this period's volume, not lifetime usage."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy import text

from app.api.miniapp_pages import _serialize_service
from app.db.models import Order, UserService
from app.services.bot_user_admin import admin_renew_service
from app.services.formatting import format_bytes_ratio
from app.services.orders import pay_with_wallet
from app.services.service_period import COUNTER_DRIFT_BYTES, period_usage, period_view, trusted_period_start
from app.services.service_renewals import RenewalPendingReview
from app.services.usage_alerts import traffic_remaining_pct
from tests.test_admin_renew_respects_carry import AdminRenewFreshStartTests
from tests.test_renewal_preview import RenewalFixture

GB = 1024**3
ROOT = Path(__file__).resolve().parents[1]


def service(start: int | None, lifetime_start: int | None = None) -> UserService:
    return UserService(period_used_baseline_bytes=start, period_lifetime_baseline_bytes=lifetime_start)


class PeriodViewTests(unittest.TestCase):
    def test_renewed_period_shows_only_the_new_volume(self) -> None:
        info = {"used_traffic": int(98.5 * GB), "data_limit": int(148.5 * GB), "status": "active"}
        view = period_view(info, service(int(98.5 * GB)))
        self.assertEqual((view["used_traffic"], view["data_limit"]), (0, 50 * GB))
        self.assertEqual(info["data_limit"], int(148.5 * GB), "the panel numbers are not changed")

    def test_raw_numbers_without_a_trusted_start(self) -> None:
        info = {"used_traffic": 5 * GB, "data_limit": 60 * GB}
        cases = {
            "no start": service(None),
            "usage reset below the start": service(10 * GB),
            "limit lowered to the start": service(60 * GB),
        }
        for label, svc in cases.items():
            with self.subTest(label):
                self.assertIs(period_view(info, svc), info)
        unlimited = {"used_traffic": 5 * GB, "data_limit": 0}
        self.assertIs(period_view(unlimited, service(GB)), unlimited)

    def test_panel_reset_is_detected_by_the_lifetime_counter(self) -> None:
        svc = service(100 * GB, lifetime_start=300 * GB)
        # No reset: both counters grew by 20 GB.
        self.assertEqual(trusted_period_start(svc, used=120 * GB, limit=150 * GB, lifetime=320 * GB), 100 * GB)
        # Reset in the panel, then 110 GB used: usage is past the start again, lifetime says otherwise.
        self.assertIsNone(trusted_period_start(svc, used=110 * GB, limit=150 * GB, lifetime=410 * GB))
        self.assertEqual(
            trusted_period_start(svc, used=120 * GB, limit=150 * GB, lifetime=320 * GB + COUNTER_DRIFT_BYTES),
            100 * GB,
        )

    def test_low_volume_alert_uses_the_period_share(self) -> None:
        # Third renewal of a 50 GB plan: lifetime share 20 % left, this period 100 % left.
        svc = service(198 * GB)
        used, limit = period_usage(svc, 198 * GB, 248 * GB)
        self.assertEqual(traffic_remaining_pct(used, limit), 100.0)
        self.assertAlmostEqual(traffic_remaining_pct(*period_usage(service(None), 198 * GB, 248 * GB)), 20.16, places=1)


class RenewalPeriodTests(RenewalFixture):
    async def renew(self, *, carry: bool) -> Order:
        self.plan.carry_remaining_quota = carry
        await self.session.commit()
        order = await self.create()
        await pay_with_wallet(self.session, order, self.user)
        await self.session.refresh(self.service)
        return order

    async def test_renewal_without_carry_shows_exactly_the_plan_volume(self) -> None:
        await self.renew(carry=False)
        self.assertEqual(self.service.period_used_baseline_bytes, 3 * GB)
        view = period_view(dict(self.pg.info), self.service)
        self.assertEqual((view["used_traffic"], view["data_limit"]), (0, 10 * GB))
        self.assertEqual(self.pg.info["data_limit"], 13 * GB, "the panel write is unchanged")

    async def test_renewal_with_carry_shows_remaining_plus_plan(self) -> None:
        await self.renew(carry=True)
        view = period_view(dict(self.pg.info), self.service)
        self.assertEqual((view["used_traffic"], view["data_limit"]), (0, 17 * GB))

    async def test_retry_after_a_lost_reply_keeps_the_same_period_start(self) -> None:
        order = await self.create()
        self.pg.fail_after_write = True
        with self.assertRaises(RenewalPendingReview):
            await pay_with_wallet(self.session, order, self.user)
        self.pg.fail_after_write = False
        self.pg.info["used_traffic"] += GB  # traffic used before the retry
        await pay_with_wallet(self.session, order, self.user)
        await self.session.refresh(self.service)
        self.assertEqual(self.service.period_used_baseline_bytes, 3 * GB)
        view = period_view(dict(self.pg.info), self.service)
        self.assertEqual((view["used_traffic"], view["data_limit"]), (GB, 17 * GB))

    async def test_mini_app_card_shows_the_period(self) -> None:
        await self.renew(carry=False)
        card = _serialize_service(self.service, dict(self.pg.info))
        self.assertEqual(card["traffic_pct"], 0)
        self.assertEqual(card["traffic"], format_bytes_ratio(0, 10 * GB, joiner=" از "))


class StaffRenewalClearsPeriodTests(unittest.IsolatedAsyncioTestCase):
    # Reuses the staff-renew fixture without re-running its tests.
    asyncSetUp = AdminRenewFreshStartTests.asyncSetUp
    asyncTearDown = AdminRenewFreshStartTests.asyncTearDown

    async def test_staff_renewal_with_reset_returns_to_raw_numbers(self) -> None:
        self.service.period_used_baseline_bytes = 2 * GB
        await self.session.commit()
        with (
            patch("app.services.bot_user_admin.assert_staff_pg_user_action"),
            patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=self.pg)),
            patch("app.services.bot_user_admin.service_snapshot", AsyncMock(return_value=SimpleNamespace(service=self.service))),
            patch("app.services.bot_user_admin.release_volume_hold", AsyncMock()),
            patch("app.services.bot_user_admin._hold_quota_change", AsyncMock(return_value=None)),
            patch("app.services.pg_access.staff_user_actions", return_value={"reset_usage": True, "update": True}),
        ):
            await admin_renew_service(self.session, self.service, plan=self.carry_plan, reset_traffic=True, staff={"role": "admin"})
        await self.session.refresh(self.service)
        self.assertIsNone(self.service.period_used_baseline_bytes)


class PeriodMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_services(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = ROOT / "alembic/versions/0120_service_period_baseline.py"
        spec = importlib.util.spec_from_file_location("period_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, pg_username TEXT)"))
            conn.execute(text("INSERT INTO user_services VALUES (1, 'svc')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(
                    conn.execute(text("SELECT period_used_baseline_bytes FROM user_services")).scalar(), None
                )
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT pg_username FROM user_services")).scalar(), "svc")
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
