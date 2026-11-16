"""New PasarGuard admins always carry a valid non-owner role when the panel defines roles.

Recent PasarGuard rejects ``POST /api/admin`` without ``role_id``; making a bot user a
reseller from the users page passes neither a plan nor a role.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, ResellerProfile
from app.services.pg_admin_roles import (
    AdminRoleError,
    apply_admin_role,
    resolve_new_admin_role_id,
)
from app.services.resellers import provision_reseller

OWNER_ROLE = {"id": 1, "name": "owner", "is_owner": True}
SHOP_ROLE = {"id": 4, "name": "shop", "is_owner": False}
STAFF_ROLE = {"id": 7, "name": "staff", "is_owner": False}


class FakeRoleClient:
    def __init__(self, roles: list[dict] | Exception) -> None:
        self._roles = roles
        self.create_admin = AsyncMock(side_effect=self._create)

    async def get_admin_roles(self) -> list[dict]:
        if isinstance(self._roles, Exception):
            raise self._roles
        return list(self._roles)

    async def _create(self, payload: dict) -> dict:
        if self._roles and not isinstance(self._roles, Exception) and "role_id" not in payload:
            raise ValueError("role_id: field required")
        return {"username": payload["username"]}


class ResolveRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_non_owner_role_is_used_when_none_is_requested(self) -> None:
        rid = await resolve_new_admin_role_id(FakeRoleClient([OWNER_ROLE, SHOP_ROLE]), None)
        self.assertEqual(rid, 4)

    async def test_several_roles_without_a_choice_refuse_instead_of_guessing(self) -> None:
        with self.assertRaises(AdminRoleError):
            await resolve_new_admin_role_id(FakeRoleClient([OWNER_ROLE, SHOP_ROLE, STAFF_ROLE]), None)

    async def test_owner_only_panel_refuses(self) -> None:
        with self.assertRaises(AdminRoleError):
            await resolve_new_admin_role_id(FakeRoleClient([OWNER_ROLE]), None)

    async def test_requested_owner_role_is_refused(self) -> None:
        with self.assertRaises(AdminRoleError):
            await resolve_new_admin_role_id(FakeRoleClient([OWNER_ROLE, SHOP_ROLE]), 1)

    async def test_requested_missing_role_is_refused(self) -> None:
        with self.assertRaises(AdminRoleError):
            await resolve_new_admin_role_id(FakeRoleClient([OWNER_ROLE, SHOP_ROLE]), 99)

    async def test_requested_valid_role_is_kept(self) -> None:
        rid = await resolve_new_admin_role_id(FakeRoleClient([OWNER_ROLE, SHOP_ROLE, STAFF_ROLE]), 7)
        self.assertEqual(rid, 7)

    async def test_panel_without_roles_keeps_the_legacy_payload(self) -> None:
        for client in (FakeRoleClient([]), FakeRoleClient(TimeoutError("slow"))):
            self.assertIsNone(await resolve_new_admin_role_id(client, None))
        payload: dict = {"username": "x"}
        apply_admin_role(payload, None)
        self.assertEqual(payload, {"username": "x", "is_sudo": False})

    async def test_hostile_role_ids_are_ignored(self) -> None:
        roles = [{"id": True, "is_owner": False}, {"id": "abc"}, {"id": -3}, SHOP_ROLE]
        self.assertEqual(await resolve_new_admin_role_id(FakeRoleClient(roles), None), 4)


class ProvisionWithoutPlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'r.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=5, telegram_id=5, referral_code="r5", role="user"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _provision(self, pg: FakeRoleClient) -> dict:
        async with self.Session() as session:
            user = await session.get(BotUser, 5)
            with patch("app.services.pasarguard.get_pg", return_value=pg):
                return await provision_reseller(session, user=user, create_pg_admin=True)

    async def test_role_change_to_reseller_sends_the_only_shop_role(self) -> None:
        pg = FakeRoleClient([OWNER_ROLE, SHOP_ROLE])
        creds = await self._provision(pg)
        payload = pg.create_admin.await_args.args[0]
        self.assertEqual(payload["role_id"], 4)
        self.assertNotIn("is_sudo", payload)
        async with self.Session() as session:
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 5))
        self.assertEqual(profile.pg_role_id, 4)
        self.assertEqual(creds["pg_username"], profile.pg_admin_username)

    async def test_ambiguous_roles_refuse_before_any_admin_is_created(self) -> None:
        pg = FakeRoleClient([OWNER_ROLE, SHOP_ROLE, STAFF_ROLE])
        with self.assertRaises(ValueError):
            await self._provision(pg)
        pg.create_admin.assert_not_awaited()
        async with self.Session() as session:
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 5))
        self.assertIsNone(profile)

    async def test_failed_create_is_not_retried_without_a_role(self) -> None:
        pg = FakeRoleClient([OWNER_ROLE, SHOP_ROLE])
        pg.create_admin.side_effect = TimeoutError("panel did not answer")
        with self.assertRaises(ValueError):
            await self._provision(pg)
        self.assertEqual(pg.create_admin.await_count, 1)


if __name__ == "__main__":
    unittest.main()
