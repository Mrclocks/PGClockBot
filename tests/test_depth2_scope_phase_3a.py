"""Phase 3A — Depth-2 child Principal scope foundation tests.

Scope only: Owner → Level-1 → Level-2. No provisioning / Web / Bot / UI.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal
from app.services import org_principals as op
from app.services import org_scope
from app.services.authz import authorize, authz_from_staff
from app.services.pasarguard import PasarGuardError, get_pg_for_principal
from app.services.resource_principal import (
    resolve_resource_principal,
    resource_in_principal_scope,
)
from app.services.secret_box import encrypt_secret
from tests.postgres_support import scoped_async_engine


class Phase3ADepth2ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _full_tree(self, session: AsyncSession):
        """Owner → A{A1,A2} / B{B1,B2}."""
        owner = await op.ensure_owner_principal(session)
        a = await op.create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="l1_a",
            pg_password_enc=encrypt_secret("AaBb12!@CdEf"),
        )
        a1 = await op.create_principal(session, parent_id=int(a.id), depth=2)
        a2 = await op.create_principal(session, parent_id=int(a.id), depth=2)
        b = await op.create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="l1_b",
            pg_password_enc=encrypt_secret("BbCc34!@EfGh"),
        )
        b1 = await op.create_principal(session, parent_id=int(b.id), depth=2)
        b2 = await op.create_principal(session, parent_id=int(b.id), depth=2)
        await session.commit()
        return owner, a, a1, a2, b, b1, b2

    def _staff(self, principal: OrgPrincipal, visible: frozenset[int]) -> dict:
        return op.attach_org_principal_fields(
            {
                "role": "reseller" if int(principal.depth) > 0 else "admin",
                "web_owner": int(principal.depth) == 0,
                "pg_permissions": ["pg_users"],
            },
            principal,
            visible_principal_ids=visible,
        )

    def _authorize(self, staff: dict, resource_principal_id: int):
        ctx = authz_from_staff(staff)
        return authorize(
            authenticated=True,
            ctx=ctx,
            resource_principal_id=resource_principal_id,
            pg_permission_ok=True,
            local_safety_ok=True,
        )

    async def test_a_owner_sees_all(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, b1, b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, owner)
            self.assertEqual(
                visible,
                {owner.id, a.id, a1.id, a2.id, b.id, b1.id, b2.id},
            )

    async def test_b_a_sees_a_a1_a2(self) -> None:
        async with self.Session() as session:
            _o, a, a1, a2, _b, _b1, _b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, a)
            self.assertEqual(visible, {a.id, a1.id, a2.id})

    async def test_c_a_does_not_see_b_branch(self) -> None:
        async with self.Session() as session:
            _o, a, _a1, _a2, b, b1, b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, a)
            self.assertNotIn(b.id, visible)
            self.assertNotIn(b1.id, visible)
            self.assertNotIn(b2.id, visible)

    async def test_d_a1_sees_a1_only(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, _a2, _b, _b1, _b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, a1)
            self.assertEqual(visible, {a1.id})

    async def test_e_a1_cannot_see_a(self) -> None:
        async with self.Session() as session:
            _o, a, a1, _a2, _b, _b1, _b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, a1)
            self.assertFalse(org_scope.principal_in_scope(visible, a.id))

    async def test_f_a1_cannot_see_a2(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, a2, _b, _b1, _b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, a1)
            self.assertFalse(org_scope.principal_in_scope(visible, a2.id))

    async def test_g_a1_cannot_see_b_branch(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, _a2, b, b1, b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, a1)
            for pid in (b.id, b1.id, b2.id):
                self.assertFalse(org_scope.principal_in_scope(visible, pid))

    async def test_h_b1_cannot_see_a_branch(self) -> None:
        async with self.Session() as session:
            _o, a, a1, a2, _b, b1, _b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, b1)
            self.assertEqual(visible, {b1.id})
            for pid in (a.id, a1.id, a2.id):
                self.assertFalse(org_scope.principal_in_scope(visible, pid))

    async def test_i_depth2_owner_parent_deny(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=int(owner.id), depth=2)
            with self.assertRaises(op.OrgPrincipalError):
                op.assert_depth2_parent(owner)

    async def test_j_depth2_under_depth2_deny(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            a = await op.create_principal(session, parent_id=int(owner.id), depth=1)
            a1 = await op.create_principal(session, parent_id=int(a.id), depth=2)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=int(a1.id), depth=2)
            with self.assertRaises(op.OrgPrincipalError):
                op.assert_depth2_parent(a1)

    async def test_k_depth3_deny(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            a = await op.create_principal(session, parent_id=int(owner.id), depth=1)
            a1 = await op.create_principal(session, parent_id=int(a.id), depth=2)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=int(a1.id), depth=3)
            fake = OrgPrincipal(
                id=99, parent_id=int(a1.id), depth=3, status=op.STATUS_ACTIVE
            )
            scope = org_scope.scope_ids_for_principal(
                fake, all_principals=[owner, a, a1, fake]
            )
            self.assertEqual(scope, frozenset())

    async def test_l_inactive_parent_deny(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            a = await op.create_principal(session, parent_id=int(owner.id), depth=1)
            a.status = op.STATUS_DISABLED
            await session.flush()
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=int(a.id), depth=2)
            with self.assertRaises(op.OrgPrincipalError):
                op.assert_depth2_parent(a)

    async def test_m_resource_owned_by_a1(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, b1, _b2 = await self._full_tree(session)
            res = resolve_resource_principal(
                type("R", (), {"owner_principal_id": int(a1.id)})()
            )
            for actor, expect in (
                (owner, True),
                (a, True),
                (a1, True),
                (a2, False),
                (b, False),
                (b1, False),
            ):
                vis = await org_scope.visible_principal_ids(session, actor)
                self.assertEqual(
                    resource_in_principal_scope(
                        actor_visible_principal_ids=vis, resolution=res
                    ),
                    expect,
                    msg=f"actor depth={actor.depth} id={actor.id}",
                )
                staff = self._staff(actor, vis)
                decision = self._authorize(staff, int(a1.id))
                self.assertEqual(decision.allowed, expect)

    async def test_n_resource_owned_by_a(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, _b1, _b2 = await self._full_tree(session)
            res = resolve_resource_principal(
                type("R", (), {"owner_principal_id": int(a.id)})()
            )
            for actor, expect in (
                (owner, True),
                (a, True),
                (a1, False),  # no automatic parent-resource visibility
                (a2, False),
                (b, False),
            ):
                vis = await org_scope.visible_principal_ids(session, actor)
                self.assertEqual(
                    resource_in_principal_scope(
                        actor_visible_principal_ids=vis, resolution=res
                    ),
                    expect,
                    msg=f"actor depth={actor.depth} id={actor.id}",
                )
                staff = self._staff(actor, vis)
                decision = self._authorize(staff, int(a.id))
                self.assertEqual(decision.allowed, expect)

    async def test_o_pg_role_name_does_not_affect_scope(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, b1, b2 = await self._full_tree(session)
            # Same arbitrary PG role *label* on A1 and B1 must not merge scopes.
            a1.pg_username = "SharedRoleName"
            b1.pg_username = "SharedRoleName"
            await session.commit()
            va1 = await org_scope.visible_principal_ids(session, a1)
            vb1 = await org_scope.visible_principal_ids(session, b1)
            self.assertEqual(va1, {a1.id})
            self.assertEqual(vb1, {b1.id})
            self.assertFalse(org_scope.principal_in_scope(va1, b1.id))
            self.assertFalse(org_scope.principal_in_scope(vb1, a1.id))
            # Parent branch scopes still isolated despite shared label
            va = await org_scope.visible_principal_ids(session, a)
            vb = await org_scope.visible_principal_ids(session, b)
            self.assertNotIn(b.id, va)
            self.assertNotIn(a.id, vb)
            _ = (owner, a2, b2)

    async def test_p_missing_principal_deny(self) -> None:
        async with self.Session() as session:
            self.assertEqual(
                await org_scope.visible_principal_ids(session, None), frozenset()
            )
            self.assertFalse(org_scope.principal_in_scope(frozenset({1}), None))
            ctx = authz_from_staff({"role": "admin", "username": "x"})
            d = authorize(
                authenticated=True,
                ctx=ctx,
                resource_principal_id=1,
                pg_permission_ok=True,
            )
            self.assertFalse(d.allowed)
            self.assertEqual(d.reason, "inactive_or_missing_principal")

    async def test_level2_pg_credentials_fail_closed(self) -> None:
        """Depth-2 must not use Owner / parent / sibling PG clients."""
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            owner, a, a1, a2, b, _b1, _b2 = await self._full_tree(session)
            pg_mod._pg_principal_cache.clear()

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    self.username = username
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                parent_client = await get_pg_for_principal(
                    session, principal_id=int(a.id)
                )
            self.assertEqual(parent_client.username, "l1_a")
            # Depth-2 without own PG identity: fail closed — never reuse parent cache
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(a1.id))
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(a2.id))
            # Sibling L1 still isolated
            with patch.object(pg_mod, "PasarGuardClient", _Client):
                b_client = await get_pg_for_principal(session, principal_id=int(b.id))
            self.assertEqual(b_client.username, "l1_b")
            self.assertNotEqual(parent_client.username, b_client.username)
            # Owner env client is a different factory — L2 must not call it via this path
            self.assertNotIn(
                (int(a1.id), "l1_a"),
                pg_mod._pg_principal_cache,
            )
            _ = owner

    async def test_b_sees_b_b1_b2_only(self) -> None:
        async with self.Session() as session:
            _o, _a, _a1, _a2, b, b1, b2 = await self._full_tree(session)
            visible = await org_scope.visible_principal_ids(session, b)
            self.assertEqual(visible, {b.id, b1.id, b2.id})

    async def test_l2_under_inactive_parent_empty_scope(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, _b, _b1, _b2 = await self._full_tree(session)
            a.status = op.STATUS_DISABLED
            await session.commit()
            # Existing L2 under inactive L1 → fail closed
            self.assertEqual(
                await org_scope.visible_principal_ids(session, a1), frozenset()
            )
            self.assertEqual(
                await org_scope.visible_principal_ids(session, a2), frozenset()
            )
            # Owner still sees active nodes (disabled A excluded; children active but
            # Owner listing includes active principals only — A1/A2 still active)
            visible_owner = await org_scope.visible_principal_ids(session, owner)
            self.assertNotIn(a.id, visible_owner)
            self.assertIn(a1.id, visible_owner)


if __name__ == "__main__":
    unittest.main()
