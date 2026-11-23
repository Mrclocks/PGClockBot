"""Phase 3B — Level-1 → Level-2 child Principal provisioning tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal, OrgPrincipalProvision, OrgPrincipalWebIdentity
from app.services.org_principals import (
    DEPTH_TWO,
    STATUS_DISABLED,
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    Level2ProvisionRequest,
    child_uses_parent_or_owner_pg_credentials,
    provision_level2_child,
    scoped_idempotency_key,
)
from app.services.principal_web_identity import ROLE_PRINCIPAL
from app.services.secret_box import decrypt_secret, encrypt_secret
from tests.postgres_support import scoped_async_engine


_PG_PASSWORD = "AaBb12!@CdEf"
_CHILD_PASSWORD = "CcDd34!@EfGh"
_PARENT_PASSWORD = "PpQq56!@GhIj"


def _l1_staff(
    principal: OrgPrincipal,
    *,
    visible: frozenset[int] | None = None,
    capable: bool = True,
) -> dict:
    staff = attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"web_{principal.pg_username or principal.id}",
            "web_identity_id": int(principal.id),
            "pg_admin_username": principal.pg_username,
            "pg_is_owner": False,
            "web_owner": False,
            "pg_permissions": ["pg_users", "pg_overview"],
        },
        principal,
        visible_principal_ids=visible or frozenset({int(principal.id)}),
    )
    if capable:
        staff["pg_can_create_admin"] = True
        staff["pg_actions"] = {"admins": {"create": True}}
        staff["pg_role"] = {
            "id": 10,
            "name": "Administrator",  # arbitrary — must not affect hierarchy
            "is_owner": False,
            "permissions": {"admins": {"create": True}},
        }
    else:
        staff["pg_can_create_admin"] = False
        staff["pg_actions"] = {"admins": {"create": False}, "users": {"create": True}}
        staff["pg_role"] = {
            "id": 10,
            "name": "Operator",
            "is_owner": False,
            "permissions": {"users": {"create": True}},
        }
    return staff


def _owner_staff(owner: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": "admin",
            "web_owner": True,
            "username": "owner",
            "pg_is_owner": True,
            "pg_permissions": ["pg_admins", "pg_users"],
            "pg_can_create_admin": True,
        },
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )


def _l2_staff(child: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"web_{child.id}",
            "web_identity_id": int(child.id),
            "pg_can_create_admin": True,
            "pg_actions": {"admins": {"create": True}},
            "pg_is_owner": False,
            "web_owner": False,
        },
        child,
        visible_principal_ids=frozenset({int(child.id)}),
    )


class Phase3BChildProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._parent_pg = AsyncMock()
        self._parent_pg.create_admin = AsyncMock(return_value={"username": "child"})
        self._parent_pg.delete_admin = AsyncMock(return_value=True)
        self._parent_pg.get_admin_roles = AsyncMock(
            return_value=[
                {"id": 10, "name": "Operator", "is_owner": False},
                {"id": 11, "name": "Administrator", "is_owner": False},
                {"id": 12, "name": "CustomRoleX", "is_owner": False},
                {"id": 1, "name": "Owner", "is_owner": True},
            ]
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _patch_parent_pg(self):
        return patch(
            "app.services.pasarguard.get_pg_for_principal",
            new=AsyncMock(return_value=self._parent_pg),
        )

    def _patch_owner_env(self, username: str = "env_owner_pg"):
        return patch(
            "app.services.principal_child_provisioning._owner_env_pg_username",
            return_value=username,
        )

    async def _seed_l1(
        self, session, *, username: str = "l1_a"
    ) -> tuple[OrgPrincipal, OrgPrincipal]:
        owner = await ensure_owner_principal(session)
        a = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username=username,
            pg_password_enc=encrypt_secret(_PARENT_PASSWORD),
        )
        await session.commit()
        return owner, a

    async def test_a_l1_with_permission_creates_child(self) -> None:
        async with self.Session() as session:
            owner, a = await self._seed_l1(session)
            staff = _l1_staff(a)
            with self._patch_parent_pg(), self._patch_owner_env():
                result = await provision_level2_child(
                    session,
                    staff,
                    Level2ProvisionRequest(
                        pg_username="child_a1",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="child-a-1",
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            self.assertTrue(result.created)
            self.assertEqual(int(result.principal.depth), DEPTH_TWO)
            self.assertEqual(int(result.principal.parent_id), int(a.id))
            self.assertEqual(result.pg_username, "child_a1")
            self.assertFalse(is_owner_principal(result.principal))
            self._parent_pg.create_admin.assert_awaited_once()
            payload = self._parent_pg.create_admin.await_args.args[0]
            self.assertEqual(payload["username"], "child_a1")
            self.assertNotIn("password", payload)  # scrubbed
            ident = (
                await session.execute(
                    select(OrgPrincipalWebIdentity).where(
                        OrgPrincipalWebIdentity.principal_id == int(result.principal.id)
                    )
                )
            ).scalar_one()
            self.assertEqual(ident.web_username, "child_a1")
            self.assertTrue(ident.is_active)
            _ = owner

    async def test_b_l1_without_permission_deny(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self._patch_parent_pg(), self._patch_owner_env():
                with self.assertRaises(ChildProvisionError) as ctx:
                    await provision_level2_child(
                        session,
                        _l1_staff(a, capable=False),
                        Level2ProvisionRequest(
                            pg_username="nope",
                            pg_password=_CHILD_PASSWORD,
                            idempotency_key="deny-cap",
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(ctx.exception.code, "pg_capability_denied")
            self._parent_pg.create_admin.assert_not_awaited()

    async def test_c_l2_cannot_create(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            a1 = await create_principal(session, parent_id=int(a.id), depth=2)
            await session.commit()
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    _l2_staff(a1),
                    Level2ProvisionRequest(
                        pg_username="evil",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="l2-deny",
                    ),
                )
            self.assertEqual(ctx.exception.code, "not_level1")

    async def test_d_e_client_parent_b_ignored_child_under_a(self) -> None:
        async with self.Session() as session:
            owner, a = await self._seed_l1(session, username="l1_a")
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_b",
                pg_password_enc=encrypt_secret(_PARENT_PASSWORD),
            )
            await session.commit()
            with self._patch_parent_pg(), self._patch_owner_env():
                result = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="child_under_a",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="under-a",
                        parent_id=int(b.id),  # client tries B
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            self.assertEqual(int(result.principal.parent_id), int(a.id))
            self.assertNotEqual(int(result.principal.parent_id), int(b.id))

    async def test_f_client_depth_1_forced_to_2(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self._patch_parent_pg(), self._patch_owner_env():
                result = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="forced_depth2",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="depth-f",
                        depth=1,
                        pg_role_id=10,
                    ),
                )
            self.assertEqual(int(result.principal.depth), DEPTH_TWO)

    async def test_g_client_depth_3_deny(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="d3",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="depth-g",
                        depth=3,
                    ),
                )
            self.assertEqual(ctx.exception.code, "depth_forbidden")

    async def test_h_owner_cannot_create_l2_or_l3(self) -> None:
        async with self.Session() as session:
            owner, _a = await self._seed_l1(session)
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    _owner_staff(owner),
                    Level2ProvisionRequest(
                        pg_username="owner_l2",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="owner-l2",
                        depth=2,
                    ),
                )
            self.assertEqual(ctx.exception.code, "owner_creates_l1_only")
            with self.assertRaises(ChildProvisionError):
                await provision_level2_child(
                    session,
                    _owner_staff(owner),
                    Level2ProvisionRequest(
                        pg_username="owner_l3",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="owner-l3",
                        depth=3,
                    ),
                )

    async def test_i_j_independent_pg_identity(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self._patch_parent_pg(), self._patch_owner_env("env_owner_pg"):
                result = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="indep_child",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="indep-1",
                        pg_role_id=11,
                    ),
                )
                await session.commit()
            child = result.principal
            self.assertEqual(child.pg_username, "indep_child")
            self.assertNotEqual(child.pg_username, a.pg_username)
            self.assertNotEqual(child.pg_username, "env_owner_pg")
            self.assertFalse(child_uses_parent_or_owner_pg_credentials(child, a))
            enc = child.pg_password_enc
            self.assertIsNotNone(enc)
            self.assertNotEqual(enc, _CHILD_PASSWORD)
            self.assertNotEqual(enc, _PARENT_PASSWORD)
            self.assertEqual(decrypt_secret(enc), _CHILD_PASSWORD)
            self.assertNotEqual(decrypt_secret(enc), decrypt_secret(a.pg_password_enc))

    async def test_k_pg_failure_no_active_child(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            self._parent_pg.create_admin = AsyncMock(
                side_effect=RuntimeError("pg down")
            )
            with self._patch_parent_pg(), self._patch_owner_env():
                with self.assertRaises(ChildProvisionError) as ctx:
                    await provision_level2_child(
                        session,
                        _l1_staff(a),
                        Level2ProvisionRequest(
                            pg_username="fail_pg",
                            pg_password=_CHILD_PASSWORD,
                            idempotency_key="fail-pg",
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(ctx.exception.code, "provision_failed")
            n = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipal)
                .where(OrgPrincipal.pg_username == "fail_pg")
            )
            self.assertEqual(int(n or 0), 0)

    async def test_l_db_failure_after_pg_compensates(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self._patch_parent_pg(), self._patch_owner_env(), patch(
                "app.services.principal_child_provisioning.create_principal",
                side_effect=RuntimeError("db boom"),
            ):
                with self.assertRaises(ChildProvisionError) as ctx:
                    await provision_level2_child(
                        session,
                        _l1_staff(a),
                        Level2ProvisionRequest(
                            pg_username="comp_child",
                            pg_password=_CHILD_PASSWORD,
                            idempotency_key="comp-1",
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(ctx.exception.code, "provision_failed")
            self._parent_pg.delete_admin.assert_awaited()
            self.assertEqual(
                self._parent_pg.delete_admin.await_args.args[0], "comp_child"
            )

    async def test_m_idempotent_retry_same_result(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            req = Level2ProvisionRequest(
                pg_username="idem_child",
                pg_password=_CHILD_PASSWORD,
                idempotency_key="same-key",
                pg_role_id=10,
            )
            with self._patch_parent_pg(), self._patch_owner_env():
                first = await provision_level2_child(session, _l1_staff(a), req)
                await session.commit()
                second = await provision_level2_child(session, _l1_staff(a), req)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(int(first.principal.id), int(second.principal.id))
            self.assertEqual(self._parent_pg.create_admin.await_count, 1)
            count = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipal)
                .where(OrgPrincipal.pg_username == "idem_child")
            )
            self.assertEqual(int(count or 0), 1)

    async def test_n_other_principal_cannot_use_key(self) -> None:
        async with self.Session() as session:
            owner, a = await self._seed_l1(session, username="l1_a")
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_b",
                pg_password_enc=encrypt_secret(_PARENT_PASSWORD),
            )
            await session.commit()
            key = "shared-guess"
            with self._patch_parent_pg(), self._patch_owner_env():
                first = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="a_child",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key=key,
                        pg_role_id=10,
                    ),
                )
                await session.commit()
                # B uses same client key → different scoped key → new provision
                # must not return A's child
                other = await provision_level2_child(
                    session,
                    _l1_staff(b),
                    Level2ProvisionRequest(
                        pg_username="b_child",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key=key,
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            self.assertNotEqual(int(first.principal.id), int(other.principal.id))
            self.assertEqual(int(first.principal.parent_id), int(a.id))
            self.assertEqual(int(other.principal.parent_id), int(b.id))
            # Ledger keys are creator-scoped
            keys = list(
                (
                    await session.execute(select(OrgPrincipalProvision.idempotency_key))
                )
                .scalars()
                .all()
            )
            self.assertIn(scoped_idempotency_key(int(a.id), key), keys)
            self.assertIn(scoped_idempotency_key(int(b.id), key), keys)

    async def test_o_p_q_scope_parent_child_sibling(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self._patch_parent_pg(), self._patch_owner_env():
                r1 = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="scope_a1",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="scope-1",
                        pg_role_id=10,
                    ),
                )
                r2 = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="scope_a2",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="scope-2",
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            a1, a2 = r1.principal, r2.principal
            child_vis = await visible_principal_ids(session, a1)
            self.assertEqual(child_vis, {a1.id})
            parent_vis = await visible_principal_ids(session, a)
            self.assertEqual(parent_vis, {a.id, a1.id, a2.id})
            self.assertNotIn(a.id, child_vis)
            self.assertNotIn(a2.id, child_vis)

    async def test_r_disabled_parent_cannot_create(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            a.status = STATUS_DISABLED
            await session.commit()
            staff = _l1_staff(a)
            # Staff dict may still look active — DB reload must deny.
            staff["org_status"] = "active"
            with self._patch_parent_pg(), self._patch_owner_env():
                with self.assertRaises(ChildProvisionError) as ctx:
                    await provision_level2_child(
                        session,
                        staff,
                        Level2ProvisionRequest(
                            pg_username="from_disabled",
                            pg_password=_CHILD_PASSWORD,
                            idempotency_key="dis-1",
                            pg_role_id=10,
                        ),
                    )
            # Either inactive principal from assert_can or parent_disabled from reload
            self.assertIn(
                ctx.exception.code,
                {"parent_disabled", "inactive_or_missing_principal"},
            )

    async def test_s_pg_role_name_not_hierarchy(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session)
            with self._patch_parent_pg(), self._patch_owner_env():
                result = await provision_level2_child(
                    session,
                    _l1_staff(a),
                    Level2ProvisionRequest(
                        pg_username="named_child",
                        pg_password=_CHILD_PASSWORD,
                        idempotency_key="role-name",
                        pg_role_id=11,  # "Administrator"
                        role_name="Owner",
                    ),
                )
            self.assertEqual(int(result.principal.depth), DEPTH_TWO)
            self.assertEqual(int(result.principal.parent_id), int(a.id))
            self.assertFalse(is_owner_principal(result.principal))

    async def test_reject_parent_pg_username_reuse(self) -> None:
        async with self.Session() as session:
            _o, a = await self._seed_l1(session, username="l1_a")
            with self._patch_parent_pg(), self._patch_owner_env():
                with self.assertRaises(ChildProvisionError) as ctx:
                    await provision_level2_child(
                        session,
                        _l1_staff(a),
                        Level2ProvisionRequest(
                            pg_username="l1_a",
                            pg_password=_CHILD_PASSWORD,
                            idempotency_key="reuse-parent",
                        ),
                    )
            self.assertEqual(ctx.exception.code, "parent_pg_forbidden")


if __name__ == "__main__":
    unittest.main()
