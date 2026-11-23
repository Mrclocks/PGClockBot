"""Phase 1B — org scope + authorize foundation."""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal
from app.services import org_principals as op
from app.services import org_scope
from app.services.authz import (
    PrincipalKind,
    authz_from_staff,
    authorize,
    has_org_global_scope,
    is_explicit_org_owner,
    is_platform_admin,
)
from tests.postgres_support import scoped_async_engine


class OrgScopeAuthzPhase1BTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _tree(self, session: AsyncSession):
        owner = await op.ensure_owner_principal(session)
        a = await op.create_principal(session, parent_id=owner.id, depth=1)
        a1 = await op.create_principal(session, parent_id=a.id, depth=2)
        a2 = await op.create_principal(session, parent_id=a.id, depth=2)
        b = await op.create_principal(session, parent_id=owner.id, depth=1)
        await session.commit()
        return owner, a, a1, a2, b

    async def test_owner_sees_both_sibling_branches(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b = await self._tree(session)
            visible = await org_scope.visible_principal_ids(session, owner)
            self.assertEqual(visible, {owner.id, a.id, a1.id, a2.id, b.id})

    async def test_depth1_sees_self_and_descendants(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b = await self._tree(session)
            visible = await org_scope.visible_principal_ids(session, a)
            self.assertEqual(visible, {a.id, a1.id, a2.id})
            self.assertNotIn(b.id, visible)
            self.assertNotIn(owner.id, visible)

    async def test_sibling_isolation_a_vs_b(self) -> None:
        async with self.Session() as session:
            _owner, a, a1, a2, b = await self._tree(session)
            va = await org_scope.visible_principal_ids(session, a)
            vb = await org_scope.visible_principal_ids(session, b)
            self.assertFalse(org_scope.principal_in_scope(va, b.id))
            self.assertFalse(org_scope.principal_in_scope(vb, a.id))
            self.assertFalse(org_scope.principal_in_scope(vb, a1.id))

    async def test_a1_cannot_see_a2(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, a2, _b = await self._tree(session)
            v1 = await org_scope.visible_principal_ids(session, a1)
            self.assertEqual(v1, {a1.id})
            self.assertFalse(org_scope.principal_in_scope(v1, a2.id))

    async def test_depth_gt_2_empty_scope(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            fake = OrgPrincipal(
                parent_id=owner.id,
                depth=3,
                status=op.STATUS_ACTIVE,
            )
            # not persisted — pure function
            scope = org_scope.scope_ids_for_principal(fake, all_principals=[owner, fake])
            self.assertEqual(scope, frozenset())

    async def test_missing_principal_deny(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await org_scope.visible_principal_ids(session, None), frozenset())
            ctx = authz_from_staff({"role": "admin", "username": "x"})
            d = authorize(
                authenticated=True,
                ctx=ctx,
                resource_principal_id=1,
                pg_permission_ok=True,
            )
            self.assertFalse(d.allowed)
            self.assertEqual(d.reason, "inactive_or_missing_principal")

    async def test_inactive_principal_deny(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            a = await op.create_principal(session, parent_id=owner.id, depth=1)
            a.status = op.STATUS_DISABLED
            await session.commit()
            self.assertEqual(await org_scope.visible_principal_ids(session, a), frozenset())
            staff = op.attach_org_principal_fields(
                {"role": "reseller"}, a, visible_principal_ids=frozenset()
            )
            ctx = authz_from_staff(staff)
            d = authorize(
                authenticated=True,
                ctx=ctx,
                resource_principal_id=a.id,
                pg_permission_ok=True,
            )
            self.assertFalse(d.allowed)

    async def test_role_admin_without_owner_mapping_not_global(self) -> None:
        ctx = authz_from_staff({"role": "admin", "username": "legacy"})
        self.assertTrue(is_platform_admin(ctx))  # legacy C0
        self.assertFalse(is_explicit_org_owner(ctx))
        self.assertFalse(has_org_global_scope(ctx))
        self.assertIsNone(ctx.principal_id)

    async def test_pg_role_names_do_not_affect_scope(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b = await self._tree(session)
            a.pg_username = "same_role_label"
            b.pg_username = "same_role_label"
            await session.commit()
            va = await org_scope.visible_principal_ids(session, a)
            vb = await org_scope.visible_principal_ids(session, b)
            self.assertNotEqual(va, vb)
            self.assertFalse(org_scope.principal_in_scope(va, b.id))

    async def test_same_pg_role_different_principals_isolated(self) -> None:
        """Arbitrary PG role strings must not merge scopes."""
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            a = await op.create_principal(
                session, parent_id=owner.id, depth=1, pg_username="RoleX"
            )
            b = await op.create_principal(
                session, parent_id=owner.id, depth=1, pg_username="RoleX"
            )
            await session.commit()
            va = await org_scope.visible_principal_ids(session, a)
            vb = await org_scope.visible_principal_ids(session, b)
            self.assertEqual(va, {a.id})
            self.assertEqual(vb, {b.id})

    async def test_authorize_ok_and_missing_scope(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b = await self._tree(session)
            vis = await org_scope.visible_principal_ids(session, a)
            staff = op.attach_org_principal_fields(
                {"role": "reseller", "pg_permissions": ["pg_users"]},
                a,
                visible_principal_ids=vis,
            )
            ctx = authz_from_staff(staff)
            self.assertEqual(ctx.kind, PrincipalKind.RESELLER)
            ok = authorize(
                authenticated=True,
                ctx=ctx,
                resource_principal_id=a1.id,
                pg_permission_ok=True,
            )
            self.assertTrue(ok.allowed)
            deny_b = authorize(
                authenticated=True,
                ctx=ctx,
                resource_principal_id=b.id,
                pg_permission_ok=True,
            )
            self.assertFalse(deny_b.allowed)
            self.assertEqual(deny_b.reason, "resource_out_of_scope")
            # missing scope on context
            bare = authz_from_staff(
                {
                    "role": "reseller",
                    "org_principal_id": a.id,
                    "org_depth": 1,
                    "org_parent_id": owner.id,
                    "org_status": "active",
                    "org_visible_principal_ids": [],
                }
            )
            d = authorize(
                authenticated=True,
                ctx=bare,
                resource_principal_id=a.id,
                pg_permission_ok=True,
            )
            self.assertFalse(d.allowed)
            self.assertEqual(d.reason, "missing_scope")

    async def test_resolve_web_owner_vs_role_admin(self) -> None:
        async with self.Session() as session:
            owner = await op.resolve_org_principal_for_staff(
                session, {"role": "admin", "web_owner": True}
            )
            self.assertIsNotNone(owner)
            assert owner is not None
            self.assertTrue(op.is_owner_principal(owner))
            bare = await op.resolve_org_principal_for_staff(
                session, {"role": "admin", "username": "x"}
            )
            self.assertIsNone(bare)


if __name__ == "__main__":
    unittest.main()
