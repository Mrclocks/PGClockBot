"""Approve/provision is claimed once: a second approve must not create another PG admin."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    ResellerApplication,
    ResellerApplicationStatus,
    ResellerPlan,
    ResellerProfile,
)
from app.services.billing import GB
from app.services.resellers import (
    ALREADY_RESELLER,
    APP_IN_PROGRESS,
    approve_application,
    provision_reseller,
)


class FakePasarGuard:
    def __init__(self) -> None:
        self.admins: dict[str, dict] = {}
        self.create_calls = 0

    async def create_admin(self, payload: dict) -> dict:
        self.create_calls += 1
        name = payload["username"]
        self.admins[name] = {
            "id": len(self.admins) + 1,
            "username": name,
            "data_limit": payload.get("data_limit"),
            "max_users": payload.get("max_users"),
        }
        return self.admins[name]

    async def get_admin(self, username: str) -> dict:
        return dict(self.admins.get(username) or {})

    async def modify_admin(self, username: str, payload: dict) -> dict:
        admin = self.admins.setdefault(username, {"username": username})
        admin.update({k: v for k, v in payload.items() if v is not None})
        return dict(admin)


class ApproveIdempotentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=9, telegram_id=99, referral_code="u9", role="user", wallet_balance=0),
                ResellerPlan(
                    id=1, name="fixed", plan_kind="subscription", billing_mode="fixed",
                    duration_days=30, included_users=5, included_gb=50, price=0,
                    create_pg_admin=True, pg_role_id=2, is_active=True,
                    web_permissions='["orders"]',
                ),
                ResellerApplication(
                    id=1, user_id=9, plan_id=1,
                    status=ResellerApplicationStatus.AWAITING_APPROVAL.value,
                ),
            ])
            await session.commit()
        self.pg = FakePasarGuard()
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_new_admin_fits_owner_budget", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
            patch("app.services.resellers.get_reseller_panel_base_url", AsyncMock(return_value="https://panel.example")),
            patch("app.services.resellers.get_reseller_pg_panel_base_url", AsyncMock(return_value="")),
            patch("app.services.secret_box.encrypt_secret", return_value="enc"),
            patch("app.services.web_auth.hash_password", return_value="hash"),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def test_second_approve_does_not_create_second_admin(self) -> None:
        async with self.Session() as session:
            app = await session.get(ResellerApplication, 1)
            first = await approve_application(session, app, reviewer_tg=1)
            self.assertEqual(self.pg.create_calls, 1)
            self.assertTrue(first.get("pg_username"))
            app = await session.get(ResellerApplication, 1)
            self.assertEqual(app.status, ResellerApplicationStatus.APPROVED.value)

            again = await approve_application(session, app, reviewer_tg=1)
            self.assertEqual(self.pg.create_calls, 1)
            self.assertTrue(again.get("already_provisioned"))
            self.assertEqual(again.get("pg_username"), first.get("pg_username"))
            self.assertIsNone(again.get("pg_password"))

    async def test_provision_retry_skips_create_admin_when_profile_exists(self) -> None:
        async with self.Session() as session:
            user = await session.get(BotUser, 9)
            plan = await session.get(ResellerPlan, 1)
            first = await provision_reseller(session, user=user, plan=plan)
            self.assertEqual(self.pg.create_calls, 1)
            with self.assertRaises(ValueError) as grant_err:
                await provision_reseller(session, user=user, plan=plan)
            self.assertEqual(str(grant_err.exception), ALREADY_RESELLER)
            again = await provision_reseller(session, user=user, plan=plan, reuse_existing=True)
            self.assertEqual(self.pg.create_calls, 1)
            self.assertTrue(again.get("already_provisioned"))
            self.assertEqual(again.get("pg_username"), first.get("pg_username"))
            profiles = (
                await session.execute(
                    __import__("sqlalchemy").select(ResellerProfile).where(ResellerProfile.user_id == 9)
                )
            ).scalars().all()
            self.assertEqual(len(profiles), 1)

    async def test_fresh_provisioning_lease_blocks_second_approve(self) -> None:
        from datetime import datetime, timezone

        async with self.Session() as session:
            session.add_all([
                BotUser(id=10, telegram_id=100, referral_code="u10", role="user", wallet_balance=0),
                ResellerApplication(
                    id=2, user_id=10, plan_id=1,
                    status=ResellerApplicationStatus.PROVISIONING.value,
                    updated_at=datetime.now(timezone.utc),
                ),
            ])
            await session.commit()
            stuck = await session.get(ResellerApplication, 2)
            with self.assertRaises(ValueError) as caught:
                await approve_application(session, stuck, reviewer_tg=2)
            self.assertEqual(str(caught.exception), APP_IN_PROGRESS)
            self.assertEqual(self.pg.create_calls, 0)


if __name__ == "__main__":
    unittest.main()
