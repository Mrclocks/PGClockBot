"""Phase 1 FINAL SECURITY GATE — C2 / M4 / H4 / outage / shortcuts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, Role
from app.services.authz import (
    authz_from_staff,
    has_org_global_scope,
    is_explicit_org_owner,
    is_platform_admin,
)
from app.services.org_principals import (
    attach_org_principal_fields,
    ensure_owner_principal,
    resolve_org_principal_for_staff,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff


class C2OwnerPrincipalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_a_role_admin_alone_not_global(self) -> None:
        staff = {"role": "admin", "username": "legacy"}
        ctx = authz_from_staff(staff)
        self.assertTrue(is_platform_admin(ctx))  # legacy shop label
        self.assertFalse(is_explicit_org_owner(ctx))
        self.assertFalse(has_org_global_scope(ctx))
        self.assertFalse(is_explicit_owner_staff(staff))
        async with self.Session() as session:
            principal = await resolve_org_principal_for_staff(session, staff)
            self.assertIsNone(principal)

    async def test_b_explicit_owner_has_global_scope(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = attach_org_principal_fields(
                {"role": "admin", "web_owner": True, "username": "owner"},
                owner,
                visible_principal_ids=await visible_principal_ids(session, owner),
            )
            ctx = authz_from_staff(staff)
            self.assertTrue(is_explicit_org_owner(ctx))
            self.assertTrue(has_org_global_scope(ctx))
            self.assertTrue(is_explicit_owner_staff(staff))

    async def test_c_web_and_bot_same_owner_principal(self) -> None:
        from app.bot.auth import is_bot_owner_principal, resolve_bot_owner_principal

        async with self.Session() as session:
            web_owner = await ensure_owner_principal(session)
            bot_user = BotUser(
                telegram_id=111001,
                role=Role.ADMIN.value,
                referral_code="own1",
                wallet_balance=0,
            )
            session.add(bot_user)
            await session.commit()
            await session.refresh(bot_user)

            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={111001}),
            ):
                self.assertTrue(
                    await is_bot_owner_principal(session, bot_user, is_reseller_bot=False)
                )
                bot_owner = await resolve_bot_owner_principal(
                    session, bot_user, is_reseller_bot=False
                )
            self.assertIsNotNone(bot_owner)
            self.assertEqual(int(bot_owner.id), int(web_owner.id))

    async def test_g_missing_principal_deny(self) -> None:
        self.assertFalse(is_explicit_owner_staff({"role": "admin"}))
        self.assertFalse(is_explicit_owner_staff({"role": "admin", "org_depth": 1}))
        self.assertFalse(
            has_org_global_scope(authz_from_staff({"role": "admin", "org_depth": 0}))
        )

    async def test_h_missing_scope_deny(self) -> None:
        # Depth 0 but empty visible set → not global
        staff = {
            "role": "admin",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_status": "active",
            "org_visible_principal_ids": [],
        }
        self.assertTrue(is_explicit_owner_staff(staff))
        self.assertFalse(has_org_global_scope(authz_from_staff(staff)))


class H4BotOwnerGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_d_non_owner_callback_denied(self) -> None:
        from app.bot.auth import is_bot_owner_principal

        async with self.Session() as session:
            user = BotUser(
                telegram_id=222,
                role=Role.USER.value,
                referral_code="u222",
                wallet_balance=0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            self.assertFalse(
                await is_bot_owner_principal(session, user, is_reseller_bot=False)
            )

    async def test_e_shop_bot_denied(self) -> None:
        from app.bot.auth import is_bot_owner_principal

        async with self.Session() as session:
            await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=333,
                role=Role.ADMIN.value,
                referral_code="u333",
                wallet_balance=0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={333}),
            ):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, user, is_reseller_bot=True
                    )
                )

    async def test_f_backup_gate_uses_owner_middleware(self) -> None:
        src = open("app/bot/__init__.py", encoding="utf-8").read()
        self.assertIn("_RequireBotOwnerPrincipal", src)
        self.assertIn("admin_backup.router", src)
        self.assertIn("owner_gate", src)


class PgOutageAndCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_i_pg_unavailable_deny(self) -> None:
        from app.services.pg_staff_access import (
            PG_UNAVAILABLE_MSG,
            enforce_pg_admin_web_gate,
        )

        with patch(
            "app.services.pg_staff_access.fetch_pg_admin_gate",
            new=AsyncMock(return_value=("unreachable", None)),
        ), patch(
            "app.services.pg_staff_access.revoke_web_access", new=AsyncMock()
        ) as revoke:
            ok, msg = await enforce_pg_admin_web_gate(
                AsyncMock(), f"final_gate_{id(self)}"
            )
        self.assertFalse(ok)
        self.assertEqual(msg, PG_UNAVAILABLE_MSG)
        revoke.assert_not_called()

    async def test_j_cached_ok_does_not_create_owner_staff(self) -> None:
        # Gate cache returns allow for pg_staff username — never sets Owner fields.
        from app.services import pg_staff_access as psa

        uname = f"cache_probe_{id(self)}"
        psa._PG_GATE_CACHE[uname] = (psa.time.monotonic(), (True, None))
        ok, msg = await psa.enforce_pg_admin_web_gate(AsyncMock(), uname)
        self.assertTrue(ok)
        self.assertIsNone(msg)
        # Staff dict from cache path still has no Owner principal
        staff = {"role": "pg_staff", "pg_admin_username": uname}
        self.assertFalse(is_explicit_owner_staff(staff))
        self.assertFalse(has_org_global_scope(authz_from_staff(staff)))


class SiblingAndRoleNameTests(unittest.TestCase):
    def test_k_sibling_principals_isolated(self) -> None:
        from app.services.org_scope import scope_ids_for_principal
        from app.services.resource_principal import (
            ResourcePrincipalResolution,
            resource_in_principal_scope,
        )

        owner = SimpleNamespace(id=1, depth=0, parent_id=None, status="active")
        a = SimpleNamespace(id=10, depth=1, parent_id=1, status="active")
        b = SimpleNamespace(id=20, depth=1, parent_id=1, status="active")
        vis_b = scope_ids_for_principal(b, all_principals=[owner, a, b])
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=vis_b,
                resolution=ResourcePrincipalResolution(10, "explicit"),
            )
        )

    def test_l_pg_role_names_irrelevant(self) -> None:
        for name in ("Operator", "admin", "RoleX"):
            staff = {
                "role": "reseller",
                "bot_user_id": 10,
                "pg_role_name": name,
                "org_principal_id": 10,
                "org_depth": 1,
                "org_status": "active",
                "org_visible_principal_ids": [10],
            }
            ctx = authz_from_staff(staff)
            self.assertFalse(is_explicit_org_owner(ctx))
            self.assertFalse(has_org_global_scope(ctx))


if __name__ == "__main__":
    unittest.main()
