"""Phase 1G — final legacy authorization hardening.

A. bare role=admin cannot obtain global org / platform shop scope
B. bare BotUser.role=admin cannot become Owner
C. reseller/pg_staff without Principal → DENY on require_staff
D. sibling Principal remains isolated
E. explicit Owner still works
F. legitimate shop feature allow (SAFE LEGACY can_shop) still works
H. shop_scope platform privileges require explicit Owner (not role alone)
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, Role
from app.services.authz import (
    authz_from_staff,
    can_shop,
    has_org_global_scope,
    is_explicit_org_owner,
)
from app.services.org_principals import (
    attach_org_principal_fields,
    ensure_owner_principal,
    resolve_org_principal_for_staff,
)
from app.services.org_scope import scope_ids_for_principal, visible_principal_ids
from app.services.platform_identity import (
    is_explicit_owner_staff,
    is_web_platform_admin,
)
from app.services.resource_principal import (
    ResourcePrincipalResolution,
    resource_in_principal_scope,
)
from app.services.shop_scope import (
    ShopScopeError,
    assert_bot_user_in_scope,
    assert_order_in_scope,
    is_platform_admin,
    resolve_shop_scope_id,
    shop_owner_id,
)
from tests.postgres_support import scoped_async_engine


class BareAdminScopeTests(unittest.TestCase):
    """A / H — role=admin alone is not platform or global org authority."""

    def test_a_bare_admin_no_global_org_scope(self) -> None:
        staff = {"role": "admin", "username": "legacy"}
        ctx = authz_from_staff(staff)
        self.assertFalse(is_explicit_org_owner(ctx))
        self.assertFalse(has_org_global_scope(ctx))
        self.assertFalse(is_explicit_owner_staff(staff))
        self.assertFalse(is_platform_admin(staff))
        self.assertIsNone(shop_owner_id(staff))
        with self.assertRaises(ShopScopeError):
            resolve_shop_scope_id(staff)
        with self.assertRaises(ShopScopeError):
            assert_order_in_scope(staff, SimpleNamespace(reseller_id=None))
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff, SimpleNamespace(reseller_id=None))

    def test_a_web_platform_admin_label_is_safe_legacy(self) -> None:
        # Identity label only — must not equal shop_scope platform privilege.
        self.assertTrue(is_web_platform_admin({"role": "admin"}))
        self.assertFalse(is_platform_admin({"role": "admin"}))

    def test_h_bare_admin_cannot_bypass_tenant_assert(self) -> None:
        bare = {"role": "admin"}
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(bare, SimpleNamespace(reseller_id=10))


class ExplicitOwnerWorksTests(unittest.IsolatedAsyncioTestCase):
    """E — explicit Owner Principal retains platform shop privileges."""

    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_e_explicit_owner_platform_shop(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = attach_org_principal_fields(
                {"role": "admin", "web_owner": True, "username": "owner"},
                owner,
                visible_principal_ids=await visible_principal_ids(session, owner),
            )
            self.assertTrue(is_platform_admin(staff))
            self.assertTrue(is_explicit_owner_staff(staff))
            self.assertIsNone(shop_owner_id(staff))
            self.assertIsNone(resolve_shop_scope_id(staff))
            assert_bot_user_in_scope(staff, SimpleNamespace(reseller_id=None))
            with self.assertRaises(ShopScopeError):
                assert_bot_user_in_scope(staff, SimpleNamespace(reseller_id=99))


class StickyBotAdminOwnerTests(unittest.IsolatedAsyncioTestCase):
    """B — sticky BotUser.role=admin is not Owner without ADMIN_IDS."""

    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_b_sticky_role_admin_not_owner(self) -> None:
        from app.bot.auth import is_bot_owner_principal, is_platform_admin

        async with self.Session() as session:
            await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=900001,
                role=Role.ADMIN.value,
                referral_code="sticky1",
                wallet_balance=0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            # Legacy shop/platform-admin candidate label may still be true…
            self.assertTrue(is_platform_admin(user))
            # …but Owner gate requires ADMIN_IDS membership.
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids=set()),
            ):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, user, is_reseller_bot=False
                    )
                )

    async def test_b_admin_ids_owner_works(self) -> None:
        from app.bot.auth import is_bot_owner_principal

        async with self.Session() as session:
            await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=4242,
                role=Role.USER.value,
                referral_code="aid1",
                wallet_balance=0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={4242}),
            ):
                self.assertTrue(
                    await is_bot_owner_principal(
                        session, user, is_reseller_bot=False
                    )
                )


class PrincipalAttachDenyTests(unittest.IsolatedAsyncioTestCase):
    """C — reseller/pg_staff security-sensitive path without Principal → DENY."""

    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_c_reseller_without_principal_resolves_none(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            await session.commit()
            staff = {"role": "reseller", "bot_user_id": 555}
            self.assertIsNone(
                await resolve_org_principal_for_staff(session, staff)
            )

    async def test_c_pg_staff_without_principal_resolves_none(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            await session.commit()
            staff = {
                "role": "pg_staff",
                "pg_staff_id": 77,
                "pg_admin_username": "ghost",
            }
            self.assertIsNone(
                await resolve_org_principal_for_staff(session, staff)
            )


class SiblingIsolationTests(unittest.TestCase):
    """D — sibling Principals remain isolated."""

    def test_d_sibling_isolation(self) -> None:
        owner = SimpleNamespace(id=1, depth=0, parent_id=None, status="active")
        a = SimpleNamespace(id=10, depth=1, parent_id=1, status="active")
        b = SimpleNamespace(id=20, depth=1, parent_id=1, status="active")
        vis_a = scope_ids_for_principal(a, all_principals=[owner, a, b])
        vis_b = scope_ids_for_principal(b, all_principals=[owner, a, b])
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=vis_a,
                resolution=ResourcePrincipalResolution(20, "explicit"),
            )
        )
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=vis_b,
                resolution=ResourcePrincipalResolution(10, "explicit"),
            )
        )
        staff_a = {"role": "reseller", "bot_user_id": 10}
        staff_b = {"role": "reseller", "bot_user_id": 20}
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff_a, SimpleNamespace(reseller_id=20))
        with self.assertRaises(ShopScopeError):
            assert_bot_user_in_scope(staff_b, SimpleNamespace(reseller_id=10))


class SafeLegacyShopFeatureTests(unittest.TestCase):
    """F — harmless shop feature allow for role=admin remains (SAFE LEGACY)."""

    def test_f_can_shop_admin_feature_allow(self) -> None:
        ctx = authz_from_staff({"role": "admin"})
        self.assertTrue(can_shop(ctx, "orders"))
        self.assertTrue(can_shop(ctx, "plans"))
        # Must still not imply Owner / platform shop_scope.
        self.assertFalse(is_explicit_org_owner(ctx))
        self.assertFalse(is_platform_admin({"role": "admin"}))


if __name__ == "__main__":
    unittest.main()
