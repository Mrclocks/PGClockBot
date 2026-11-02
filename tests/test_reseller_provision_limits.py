"""A reseller bought on a fixed package is limited in PasarGuard from the first moment, never unlimited."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, PgAdminSubscription, ResellerPlan
from app.services.billing_suspend import _set_admin_enabled
from app.services.pg_admin_subscription import _apply_pg_capacity
from app.services.resellers import provision_reseller

GB = 1024**3
ADMIN_FIELDS = {
    "password", "telegram_id", "discord_webhook", "status", "data_limit", "sub_template", "sub_domain",
    "profile_title", "support_url", "custom_variables", "note", "notification_enable", "role_id",
    "permission_overrides",
}
OVERRIDE_KEYS = ("max_users", "data_limit_min", "data_limit_max", "expire_min", "expire_max",
                 "min_hwid_per_user", "max_hwid_per_user", "on_hold_timeout_min", "on_hold_timeout_max")


class FakePasarGuard:
    """Admin create/update as PasarGuard does them: unknown keys ignored, ``None`` leaves a field,
    ``data_limit`` <= 0 or missing means unlimited, ``permission_overrides`` replaced whole."""

    def __init__(self, *, owner_gb: int = 0, fail_update: bool = False) -> None:
        self.admins: dict[str, dict] = {"owner": {"id": 1, "username": "owner", "data_limit": owner_gb * GB or None}}
        self.fail_update = fail_update
        self.create_admin = AsyncMock(side_effect=self._create)

    @staticmethod
    def _overrides(value: dict | None) -> dict:
        return {key: (value or {}).get(key) for key in OVERRIDE_KEYS}

    async def _create(self, payload: dict) -> dict:
        limit = payload.get("data_limit")
        self.admins[payload["username"]] = {
            "id": len(self.admins) + 1, "username": payload["username"], "status": "active",
            "data_limit": limit if limit and limit > 0 else None,
            "permission_overrides": self._overrides(payload.get("permission_overrides")),
        }
        return self.admins[payload["username"]]

    async def get_admin(self, username: str) -> dict:
        return dict(self.admins.get(username) or {})

    async def modify_admin(self, username: str, payload: dict) -> dict:
        if self.fail_update:
            raise TimeoutError("panel did not answer")
        admin = self.admins[username]
        for key, value in payload.items():
            if key not in ADMIN_FIELDS or value is None:
                continue
            if key == "data_limit":
                admin["data_limit"] = value if value > 0 else None
            elif key == "permission_overrides":
                admin["permission_overrides"] = self._overrides(value)
            else:
                admin[key] = value
        return dict(admin)


class ProvisionCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'p.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=5, telegram_id=5, referral_code="r5", role="user"))
            session.add(ResellerPlan(
                id=1, name="fixed", price=100, plan_kind="subscription", billing_mode="fixed", duration_days=30,
                included_gb=20, included_users=10, pg_role_id=3, create_pg_admin=True,
            ))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def sell_others(self, gb: int) -> None:
        async with self.Session() as session:
            session.add(PgAdminSubscription(pg_username="other", base_gb=gb, base_users=0, access_status="active"))
            await session.commit()

    async def provision(self, pg: FakePasarGuard) -> dict:
        from app.config import get_settings

        real = get_settings()
        settings = SimpleNamespace(**{k: getattr(real, k) for k in dir(real) if not k.startswith("_")})
        settings.pg_username = "owner"
        async with self.Session() as session:
            user = await session.get(BotUser, 5)
            plan = await session.get(ResellerPlan, 1)
            with patch("app.services.pasarguard.get_pg", return_value=pg), \
                    patch("app.config.get_settings", return_value=settings):
                return await provision_reseller(session, user=user, plan=plan, panel_base_url="https://panel")

    async def subscription(self, pg_username: str) -> PgAdminSubscription | None:
        async with self.Session() as session:
            return await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == pg_username))


class ProvisionLimitTests(ProvisionCase):
    async def test_the_new_admin_is_created_with_the_package_limits(self) -> None:
        pg = FakePasarGuard(owner_gb=100)
        creds = await self.provision(pg)
        admin = pg.admins[creds["pg_username"]]
        self.assertEqual(admin["data_limit"], 20 * GB)
        self.assertEqual(admin["permission_overrides"]["max_users"], 10)
        sub = await self.subscription(creds["pg_username"])
        self.assertEqual((sub.base_gb, sub.base_users), (20, 10))
        self.assertIsNotNone(sub.expires_at)

    async def test_an_owner_budget_too_small_refuses_before_anything_is_created(self) -> None:
        await self.sell_others(90)
        pg = FakePasarGuard(owner_gb=100)
        with self.assertRaises(ValueError) as ctx:
            await self.provision(pg)
        self.assertIn("سقف حجم ادمین اصلی", str(ctx.exception))
        pg.create_admin.assert_not_awaited()

    async def test_a_panel_failure_after_creation_leaves_a_limited_admin_with_a_period(self) -> None:
        pg = FakePasarGuard(owner_gb=100, fail_update=True)
        creds = await self.provision(pg)
        self.assertEqual(pg.admins[creds["pg_username"]]["data_limit"], 20 * GB)
        sub = await self.subscription(creds["pg_username"])
        self.assertIsNotNone(sub)
        self.assertIsNotNone(sub.expires_at)
        self.assertEqual(creds["plan_name"], "fixed")

    async def test_an_owner_without_a_panel_limit_does_not_block(self) -> None:
        await self.sell_others(5000)
        creds = await self.provision(FakePasarGuard(owner_gb=0))
        self.assertTrue(creds["pg_username"])


class PanelWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabling_and_enabling_an_admin_actually_changes_its_status(self) -> None:
        pg = FakePasarGuard()
        await _set_admin_enabled(pg, "owner", enabled=False)
        self.assertEqual(pg.admins["owner"]["status"], "disabled")
        await _set_admin_enabled(pg, "owner", enabled=True)
        self.assertEqual(pg.admins["owner"]["status"], "active")

    async def test_a_capacity_sync_keeps_the_admins_other_overrides(self) -> None:
        pg = FakePasarGuard()
        await pg._create({"username": "shop", "permission_overrides": {"max_hwid_per_user": 3, "max_users": 5}})
        await _apply_pg_capacity(pg, "shop", total_gb=30, total_users=12)
        admin = pg.admins["shop"]
        self.assertEqual(admin["data_limit"], 30 * GB)
        self.assertEqual(admin["permission_overrides"]["max_users"], 12)
        self.assertEqual(admin["permission_overrides"]["max_hwid_per_user"], 3)

    async def test_a_zero_total_never_writes_unlimited(self) -> None:
        pg = FakePasarGuard()
        await pg._create({"username": "shop", "data_limit": 7 * GB})
        await _apply_pg_capacity(pg, "shop", total_gb=0, total_users=0)
        self.assertEqual(pg.admins["shop"]["data_limit"], 7 * GB)


if __name__ == "__main__":
    unittest.main()
