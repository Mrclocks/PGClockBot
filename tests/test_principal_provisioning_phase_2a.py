"""Phase 2A — Level-1 Principal provisioning foundation tests."""

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
    attach_org_principal_fields,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.principal_provisioning import (
    Level1ProvisionRequest,
    PrincipalProvisionError,
    principal_uses_owner_pg_credentials,
    provision_level1_principal,
)
from tests.postgres_support import scoped_async_engine

# PasarGuard-compatible password (policy: 12+, 2 digit, 2 upper, 2 lower, special)
_PG_PASSWORD = "AaBb12!@CdEf"


def _owner_staff(owner: OrgPrincipal, *, pg_capable: bool = True) -> dict:
    staff = attach_org_principal_fields(
        {"role": "admin", "web_owner": True, "username": "owner"},
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )
    if pg_capable:
        staff["pg_is_owner"] = True
        staff["pg_permissions"] = ["pg_admins", "pg_users", "pg_overview"]
    else:
        staff["pg_is_owner"] = False
        staff["pg_permissions"] = ["pg_users", "pg_overview"]
    return staff


class Phase2AProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._pg = AsyncMock()
        self._pg.create_admin = AsyncMock(return_value={"username": "x"})
        self._pg.delete_admin = AsyncMock(return_value=True)
        self._pg.get_admin_roles = AsyncMock(
            return_value=[
                {"id": 10, "name": "Operator", "is_owner": False},
                {"id": 11, "name": "Administrator", "is_owner": False},
                {"id": 12, "name": "CustomRoleX", "is_owner": False},
                {"id": 1, "name": "Owner", "is_owner": True},
            ]
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _patch_pg(self):
        return patch(
            "app.services.pasarguard.get_pg",
            return_value=self._pg,
        )

    def _patch_owner_env(self, username: str = "env_owner_pg"):
        return patch(
            "app.services.principal_provisioning._owner_env_pg_username",
            return_value=username,
        )

    async def test_a_owner_can_create_level1(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with self._patch_pg(), self._patch_owner_env():
                result = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="principal_a",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-a-1",
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            self.assertTrue(result.created)
            self.assertEqual(result.principal.depth, 1)
            self.assertEqual(result.principal.parent_id, owner.id)
            self.assertEqual(result.pg_username, "principal_a")
            self.assertFalse(is_owner_principal(result.principal))
            self._pg.create_admin.assert_awaited_once()
            payload = self._pg.create_admin.await_args.args[0]
            self.assertEqual(payload["username"], "principal_a")
            self.assertEqual(payload["role_id"], 10)
            self.assertNotIn("is_sudo", payload)

            ident = (
                await session.execute(
                    select(OrgPrincipalWebIdentity).where(
                        OrgPrincipalWebIdentity.principal_id == int(result.principal.id)
                    )
                )
            ).scalar_one()
            self.assertEqual(ident.web_username, "principal_a")
            self.assertTrue(ident.is_active)

    async def test_b_owner_without_pg_capability_denied(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner, pg_capable=False)
            with self._patch_pg(), self._patch_owner_env():
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="nope",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="prov-b-1",
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(ctx.exception.code, "pg_capability_denied")
            self._pg.create_admin.assert_not_awaited()

    async def test_c_non_owner_denied(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = {
                "role": "reseller",
                "bot_user_id": 9,
                "org_principal_id": 99,
                "org_depth": 1,
                "org_parent_id": owner.id,
                "org_status": "active",
                "pg_is_owner": True,
                "pg_permissions": ["pg_admins"],
            }
            with self._patch_pg(), self._patch_owner_env():
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="sibling_try",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="prov-c-1",
                        ),
                    )
            self.assertEqual(ctx.exception.code, "not_owner")
            bare = {"role": "admin", "username": "legacy"}
            with self.assertRaises(PrincipalProvisionError) as ctx2:
                await provision_level1_principal(
                    session,
                    bare,
                    Level1ProvisionRequest(
                        pg_username="bare",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-c-2",
                    ),
                )
            self.assertEqual(ctx2.exception.code, "not_owner")

    async def test_d_client_parent_id_ignored(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with self._patch_pg(), self._patch_owner_env():
                result = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="p_parent",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-d-1",
                        pg_role_id=10,
                        parent_id=99999,  # attempt override
                    ),
                )
                await session.commit()
            self.assertEqual(result.principal.parent_id, owner.id)
            self.assertNotEqual(result.principal.parent_id, 99999)

    async def test_e_client_depth_ignored(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with self._patch_pg(), self._patch_owner_env():
                result = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="p_depth",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-e-1",
                        pg_role_id=10,
                        depth=0,  # attempt to become Owner
                    ),
                )
                await session.commit()
            self.assertEqual(result.principal.depth, 1)
            self.assertFalse(is_owner_principal(result.principal))

    async def test_f_g_isolated_scope_siblings(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with self._patch_pg(), self._patch_owner_env():
                a = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="scope_a",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-f-a",
                        pg_role_id=10,
                    ),
                )
                b = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="scope_b",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-f-b",
                        pg_role_id=11,
                    ),
                )
                await session.commit()
            self.assertEqual(a.visible_principal_ids, frozenset({int(a.principal.id)}))
            self.assertEqual(b.visible_principal_ids, frozenset({int(b.principal.id)}))
            self.assertNotIn(int(owner.id), a.visible_principal_ids)
            self.assertNotIn(int(b.principal.id), a.visible_principal_ids)
            self.assertNotIn(int(a.principal.id), b.visible_principal_ids)
            # Live recompute matches
            va = await visible_principal_ids(session, a.principal)
            vb = await visible_principal_ids(session, b.principal)
            self.assertEqual(va, frozenset({int(a.principal.id)}))
            self.assertEqual(vb, frozenset({int(b.principal.id)}))

    async def test_h_no_owner_pg_credentials(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with self._patch_pg(), self._patch_owner_env("env_owner_pg"):
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="env_owner_pg",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="prov-h-1",
                            pg_role_id=10,
                        ),
                    )
                self.assertEqual(ctx.exception.code, "owner_pg_forbidden")
                result = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="own_creds",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-h-2",
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            self.assertFalse(principal_uses_owner_pg_credentials(result.principal))
            self.assertNotEqual(
                (result.principal.pg_username or "").lower(), "env_owner_pg"
            )
            # Owner principal itself has no inherited PG username from provision
            self.assertIsNone(owner.pg_username)

    async def test_i_pg_failure_no_orphan_principal(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            self._pg.create_admin = AsyncMock(side_effect=RuntimeError("pg down"))
            with self._patch_pg(), self._patch_owner_env():
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="orphan_try",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="prov-i-1",
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(ctx.exception.code, "provision_failed")
            count = await session.scalar(
                select(func.count()).select_from(OrgPrincipal).where(
                    OrgPrincipal.depth == 1
                )
            )
            self.assertEqual(int(count or 0), 0)
            prov = await session.scalar(
                select(func.count()).select_from(OrgPrincipalProvision)
            )
            self.assertEqual(int(prov or 0), 0)
            self._pg.delete_admin.assert_not_awaited()

    async def test_i2_db_failure_compensates_pg(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)

            real_create = None
            from app.services import org_principals as op

            real_create = op.create_principal

            async def boom(*args, **kwargs):
                raise RuntimeError("db explode")

            with self._patch_pg(), self._patch_owner_env(), patch(
                "app.services.principal_provisioning.create_principal",
                side_effect=boom,
            ):
                with self.assertRaises(PrincipalProvisionError):
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="comp_try",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="prov-i2-1",
                            pg_role_id=10,
                        ),
                    )
            self._pg.create_admin.assert_awaited()
            self._pg.delete_admin.assert_awaited_once_with("comp_try")
            count = await session.scalar(
                select(func.count()).select_from(OrgPrincipal).where(
                    OrgPrincipal.depth == 1
                )
            )
            self.assertEqual(int(count or 0), 0)
            _ = real_create

    async def test_j_idempotent_retry(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            req = Level1ProvisionRequest(
                pg_username="idem_user",
                pg_password=_PG_PASSWORD,
                idempotency_key="prov-j-same",
                pg_role_id=10,
            )
            with self._patch_pg(), self._patch_owner_env():
                first = await provision_level1_principal(session, staff, req)
                await session.commit()
                second = await provision_level1_principal(session, staff, req)
                await session.commit()
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(int(first.principal.id), int(second.principal.id))
            self.assertEqual(self._pg.create_admin.await_count, 1)
            n = await session.scalar(
                select(func.count()).select_from(OrgPrincipal).where(
                    OrgPrincipal.pg_username == "idem_user"
                )
            )
            self.assertEqual(int(n or 0), 1)

    async def test_k_l_m_arbitrary_role_names(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            cases = [
                ("role_custom", 12, "CustomRoleX", "prov-k-1"),
                ("role_admin_str", 11, "Administrator", "prov-l-1"),
                ("role_operator", 10, "Operator", "prov-m-1"),
            ]
            with self._patch_pg(), self._patch_owner_env():
                for uname, rid, rname, key in cases:
                    result = await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username=uname,
                            pg_password=_PG_PASSWORD,
                            idempotency_key=key,
                            pg_role_id=rid,
                            role_name=rname,
                        ),
                    )
                    self.assertEqual(result.principal.depth, 1)
                    self.assertFalse(is_owner_principal(result.principal))
                    self.assertEqual(result.visible_principal_ids, frozenset({int(result.principal.id)}))
                await session.commit()
            # Owner-equivalent PG role still rejected (local safety, not name-based).
            with self._patch_pg(), self._patch_owner_env():
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="role_owner_pg",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="prov-owner-role",
                            pg_role_id=1,
                            role_name="Owner",
                        ),
                    )
            self.assertEqual(ctx.exception.code, "owner_role_forbidden")

    async def test_n_new_principal_is_not_owner(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with self._patch_pg(), self._patch_owner_env():
                result = await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="not_owner",
                        pg_password=_PG_PASSWORD,
                        idempotency_key="prov-n-1",
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            self.assertFalse(is_owner_principal(result.principal))
            self.assertEqual(result.principal.depth, 1)
            self.assertIsNotNone(result.principal.parent_id)
            self.assertNotEqual(result.principal.id, owner.id)


if __name__ == "__main__":
    unittest.main()
