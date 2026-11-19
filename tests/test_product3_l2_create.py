"""Product 3 — L1 → L2 create UI wrapping provision_level2_child()."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import (
    OrgPrincipal,
    OrgPrincipalProvision,
    OrgPrincipalWebIdentity,
    ResellerProfile,
)
from app.services.org_principals import (
    DEPTH_TWO,
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
)
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_child_provisioning import (
    child_uses_parent_or_owner_pg_credentials,
    provision_level2_child,
)
from app.services.principal_web_identity import ROLE_PRINCIPAL
from app.services.secret_box import decrypt_secret, encrypt_secret
from tests.postgres_support import scoped_async_engine


ROOT = Path(__file__).resolve().parents[1]
_PARENT_PASSWORD = "PpQq56!@GhIj"
_CHILD_PASSWORD = "CcDd34!@EfGh"
_OWNER_ENV_PG = "env_owner_pg"
_PARENT_PG_ROLES = [
    {"id": 10, "name": "Operator", "is_owner": False},
    {"id": 11, "name": "Administrator", "is_owner": False},
    {"id": 12, "name": "CustomRoleX", "is_owner": False},
    {"id": 1, "name": "Owner", "is_owner": True},
]
_PLATFORM_ROLES = [
    {"id": 10, "name": "Operator", "is_owner": False},
    {"id": 2, "name": "Administrator", "is_owner": False},
]


def _l1_staff(principal: OrgPrincipal, *, capable: bool = True) -> dict:
    staff = attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"web_{principal.pg_username or principal.id}",
            "web_identity_id": int(principal.id),
            "pg_admin_username": principal.pg_username,
            "pg_is_owner": False,
            "web_owner": False,
            "pg_permissions": ["pg_users", "pg_overview"],
            "pg_role_name": "Operator",
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )
    if capable:
        staff["pg_can_create_admin"] = True
        staff["pg_actions"] = {"admins": {"create": True}}
        staff["pg_role"] = {
            "id": 10,
            "name": "Administrator",
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


class ParentPg:
    def __init__(self) -> None:
        self.create_admin = AsyncMock(return_value={"username": "child"})
        self.delete_admin = AsyncMock(return_value=True)
        self.get_admin_roles = AsyncMock(return_value=list(_PARENT_PG_ROLES))


class PlatformPg:
    async def get_admin_roles(self):
        return list(_PLATFORM_ROLES)


class Product3L2CreateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._parent_pg = ParentPg()
        self._pg = patch("app.services.pasarguard.get_pg", return_value=PlatformPg())
        self._parent = patch(
            "app.services.pasarguard.get_pg_for_principal",
            new=AsyncMock(return_value=self._parent_pg),
        )
        self._owner_env = patch(
            "app.services.principal_child_provisioning._owner_env_pg_username",
            return_value=_OWNER_ENV_PG,
        )
        self._features = patch(
            "app.services.pg_access.resolve_reseller_pg_features",
            new=AsyncMock(
                return_value=(["pg_users"], {"name": "CustomRoleX", "id": 12})
            ),
        )
        self._pg.start()
        self._parent.start()
        self._owner_env.start()
        self._features.start()

    async def asyncTearDown(self) -> None:
        self._features.stop()
        self._owner_env.stop()
        self._parent.stop()
        self._pg.stop()
        await self.engine.dispose()

    async def _seed(self):
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_a",
                pg_password_enc=encrypt_secret(_PARENT_PASSWORD),
            )
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_b",
                pg_password_enc=encrypt_secret(_PARENT_PASSWORD),
            )
            a1 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="existing_a1",
                pg_password_enc=encrypt_secret(_CHILD_PASSWORD),
            )
            await session.commit()
            return owner, a, b, a1

    def _app_for(self, staff: dict) -> FastAPI:
        from app.api.app import render
        from app.api.principal_pages import register_principal_pages

        app = FastAPI()

        async def require_staff():
            return staff

        async def require_admin():
            if staff.get("role") != "admin" or not is_explicit_owner_staff(staff):
                raise HTTPException(status_code=403, detail="forbidden")
            return staff

        async def get_db():
            async with self.Session() as session:
                yield session

        register_principal_pages(
            app,
            render=render,
            require_admin=require_admin,
            require_staff=require_staff,
            get_db=get_db,
        )
        return app

    def _client(self, staff: dict):
        transport = httpx.ASGITransport(app=self._app_for(staff))
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    def _form(
        self,
        *,
        username: str = "child_new1",
        role_id: str = "12",
        parent_id: str | None = None,
        depth: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        data = {
            "pg_username": username,
            "pg_password": _CHILD_PASSWORD,
            "pg_role_id": role_id,
            "note": "ui-l2",
        }
        if parent_id is not None:
            data["parent_id"] = parent_id
        if depth is not None:
            data["depth"] = depth
        if extra:
            data.update(extra)
        return data

    async def _count_by_username(self, username: str) -> int:
        async with self.Session() as session:
            n = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipal)
                .where(OrgPrincipal.pg_username == username)
            )
        return int(n or 0)

    async def test_l1_creates_own_l2(self) -> None:
        _, a, _, _ = await self._seed()
        staff = _l1_staff(a)
        with patch(
            "app.api.principal_pages.provision_level2_child",
            wraps=provision_level2_child,
        ) as spy:
            async with self._client(staff) as client:
                resp = await client.post(
                    "/principals/create-l2",
                    data=self._form(username="child_own1"),
                    follow_redirects=False,
                )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(spy.await_count)
        loc = resp.headers.get("location") or ""
        self.assertTrue(loc.startswith("/resellers"))
        self.assertNotIn(_CHILD_PASSWORD, loc)
        self.assertNotIn(_PARENT_PASSWORD, loc)
        async with self.Session() as session:
            child = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.pg_username == "child_own1")
                )
            ).scalar_one()
            parent = await session.get(OrgPrincipal, int(a.id))
            self.assertEqual(int(child.depth), DEPTH_TWO)
            self.assertEqual(int(child.parent_id), int(a.id))
            self.assertEqual(child.status, "active")
            self.assertEqual(child.pg_username, "child_own1")
            self.assertNotEqual(child.pg_username, parent.pg_username)
            self.assertNotEqual(child.pg_username, _OWNER_ENV_PG)
            self.assertFalse(child_uses_parent_or_owner_pg_credentials(child, parent))
            self.assertEqual(decrypt_secret(child.pg_password_enc), _CHILD_PASSWORD)
            self.assertNotEqual(
                decrypt_secret(child.pg_password_enc),
                decrypt_secret(parent.pg_password_enc),
            )
            self.assertIsNotNone(child.bot_user_id)
            self.assertIsNotNone(child.reseller_profile_id)
            shop = await session.get(ResellerProfile, int(child.reseller_profile_id))
            self.assertIsNotNone(shop)
            self.assertEqual(shop.pg_admin_username, "child_own1")
            self.assertIsNone(shop.pg_admin_password_enc)
            self.assertIsNone(shop.web_username)
            webs = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipalWebIdentity)
                .where(OrgPrincipalWebIdentity.principal_id == int(child.id))
            )
            self.assertEqual(int(webs or 0), 1)
            shops = await session.scalar(select(func.count()).select_from(ResellerProfile))
            self.assertEqual(int(shops or 0), 1)
            prov = (
                await session.execute(
                    select(OrgPrincipalProvision).where(
                        OrgPrincipalProvision.principal_id == int(child.id)
                    )
                )
            ).scalar_one()
            self.assertEqual(int(prov.pg_role_id), 12)
            self.assertEqual(int(prov.created_by_principal_id), int(a.id))
        payload = self._parent_pg.create_admin.await_args.args[0]
        self.assertEqual(payload["username"], "child_own1")
        self.assertEqual(int(payload["role_id"]), 12)
        self.assertNotEqual(payload["username"], _OWNER_ENV_PG)
        self.assertNotIn("password", payload)

    async def test_l1_cannot_create_under_another_l1(self) -> None:
        _, a, b, _ = await self._seed()
        async with self._client(_l1_staff(a)) as client:
            resp = await client.post(
                "/principals/create-l2",
                data=self._form(
                    username="child_not_b",
                    parent_id=str(int(b.id)),
                    extra={
                        "org_principal_id": str(int(b.id)),
                        "web_owner": "1",
                    },
                ),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        async with self.Session() as session:
            child = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.pg_username == "child_not_b")
                )
            ).scalar_one()
            self.assertEqual(int(child.parent_id), int(a.id))
            self.assertNotEqual(int(child.parent_id), int(b.id))
            self.assertEqual(int(child.depth), DEPTH_TWO)

    async def test_l2_cannot_create(self) -> None:
        _, _, _, a1 = await self._seed()
        async with self._client(_l2_staff(a1)) as client:
            resp = await client.post(
                "/principals/create-l2",
                data=self._form(username="from_l2"),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._count_by_username("from_l2"), 0)
        self.assertFalse(self._parent_pg.create_admin.await_count)

    async def test_owner_cannot_create_l2_via_this_ui(self) -> None:
        owner, _, _, _ = await self._seed()
        async with self._client(_owner_staff(owner)) as client:
            page = await client.get("/principals", follow_redirects=False)
            resp = await client.post(
                "/principals/create-l2",
                data=self._form(username="owner_l2"),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertTrue((page.headers.get("location") or "").startswith("/resellers"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._count_by_username("owner_l2"), 0)

    async def test_inactive_l1_denied(self) -> None:
        _, a, _, _ = await self._seed()
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a.id))
            row.status = "disabled"
            await session.commit()
        staff = _l1_staff(a)
        staff["org_status"] = "active"
        async with self._client(staff) as client:
            listed = await client.get("/principals")
            resp = await client.post(
                "/principals/create-l2",
                data=self._form(username="from_inactive"),
                follow_redirects=False,
            )
        self.assertEqual(listed.status_code, 403)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._count_by_username("from_inactive"), 0)

    async def test_missing_pg_capability_denied(self) -> None:
        _, a, _, _ = await self._seed()
        staff = _l1_staff(a, capable=False)
        async with self._client(staff) as client:
            page = await client.get("/principals")
            resp = await client.post(
                "/principals/create-l2",
                data=self._form(username="no_cap"),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 403)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._count_by_username("no_cap"), 0)
        self.assertFalse(self._parent_pg.create_admin.await_count)

    async def test_parent_depth_tampering_denied(self) -> None:
        _, a, b, _ = await self._seed()
        staff = _l1_staff(a)
        async with self._client(staff) as client:
            too_deep = await client.post(
                "/principals/create-l2",
                data=self._form(username="too_deep", depth="3", parent_id=str(int(b.id))),
                follow_redirects=False,
            )
            become_owner = await client.post(
                "/principals/create-l2",
                data=self._form(username="become_own", depth="0"),
                follow_redirects=False,
            )
            ignored = await client.post(
                "/principals/create-l2",
                data=self._form(
                    username="depth_ignored",
                    depth="1",
                    parent_id=str(int(b.id)),
                ),
                follow_redirects=False,
            )
        self.assertEqual(too_deep.status_code, 403)
        self.assertEqual(become_owner.status_code, 403)
        self.assertEqual(ignored.status_code, 303)
        self.assertEqual(await self._count_by_username("too_deep"), 0)
        self.assertEqual(await self._count_by_username("become_own"), 0)
        async with self.Session() as session:
            child = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.pg_username == "depth_ignored")
                )
            ).scalar_one()
            self.assertEqual(int(child.depth), DEPTH_TWO)
            self.assertEqual(int(child.parent_id), int(a.id))

    async def test_role_selection_uses_live_pg_roles(self) -> None:
        _, a, _, _ = await self._seed()
        staff = _l1_staff(a)
        async with self._client(staff) as client:
            page = await client.get("/principals", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        loc = page.headers.get("location") or ""
        self.assertTrue(loc.startswith("/resellers"))
        tpl = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertIn("افزودن نماینده", tpl)
        self.assertIn('action="/resellers/create-child"', tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="depth"', tpl)
        self.assertNotIn('name="org_principal_id"', tpl)
        async with self._client(staff) as client:
            missing = await client.post(
                "/principals/create-l2",
                data=self._form(username="bad_role", role_id="999"),
                follow_redirects=False,
            )
        self.assertEqual(missing.status_code, 303)
        loc = missing.headers.get("location") or ""
        self.assertIn("err=", loc)
        self.assertEqual(await self._count_by_username("bad_role"), 0)
        self.assertFalse(self._parent_pg.create_admin.await_count)

    async def test_rollback_on_pg_and_db_failure(self) -> None:
        _, a, _, _ = await self._seed()
        staff = _l1_staff(a)
        self._parent_pg.create_admin = AsyncMock(side_effect=RuntimeError("pg down"))
        async with self._client(staff) as client:
            pg_fail = await client.post(
                "/principals/create-l2",
                data=self._form(username="fail_pg"),
                follow_redirects=False,
            )
        self.assertEqual(pg_fail.status_code, 303)
        self.assertEqual(await self._count_by_username("fail_pg"), 0)
        self.assertFalse(self._parent_pg.delete_admin.await_count)

        self._parent_pg.create_admin = AsyncMock(return_value={"username": "comp_child"})
        self._parent_pg.delete_admin = AsyncMock(return_value=True)
        with patch(
            "app.services.principal_child_provisioning.create_principal",
            side_effect=RuntimeError("db boom"),
        ):
            async with self._client(staff) as client:
                db_fail = await client.post(
                    "/principals/create-l2",
                    data=self._form(username="comp_child"),
                    follow_redirects=False,
                )
        self.assertEqual(db_fail.status_code, 303)
        self.assertEqual(await self._count_by_username("comp_child"), 0)
        self.assertTrue(self._parent_pg.delete_admin.await_count)
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertNotIn("delete_admin", pages)
        self.assertNotIn("_compensate_delete_pg_admin", pages)
        self.assertNotIn("create_admin", pages)

    async def test_creates_independent_shop_package(self) -> None:
        _, a, _, _ = await self._seed()
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertNotIn("create_web_identity", pages)
        self.assertNotIn("bind_telegram", pages)
        async with self._client(_l1_staff(a)) as client:
            resp = await client.post(
                "/principals/create-l2",
                data=self._form(username="no_auto_id"),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        async with self.Session() as session:
            child = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.pg_username == "no_auto_id")
                )
            ).scalar_one()
            self.assertIsNotNone(child.bot_user_id)
            self.assertIsNotNone(child.reseller_profile_id)
            shop = await session.get(ResellerProfile, int(child.reseller_profile_id))
            self.assertIsNotNone(shop)
            self.assertIsNone(shop.pg_admin_password_enc)
            webs = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipalWebIdentity)
                .where(OrgPrincipalWebIdentity.principal_id == int(child.id))
            )
            self.assertEqual(int(webs or 0), 1)


class Product3SourceContracts(unittest.TestCase):
    def test_wraps_existing_service_only(self) -> None:
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertIn("provision_level2_child", pages)
        self.assertIn("Level2ProvisionRequest", pages)
        self.assertIn("/principals/create-l2", pages)
        self.assertIn("has_pg_admin_create_capability", pages)
        self.assertIn("get_pg_for_principal", pages)
        self.assertNotIn("create_admin", pages)
        self.assertNotIn("delete_admin", pages)
        self.assertNotIn("bind_telegram", pages)
        self.assertNotIn("create_web_identity", pages)
        tpl = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertIn("افزودن نماینده", tpl)
        self.assertIn("/resellers/create-child", tpl)
        self.assertIn("l2_pg_roles", tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="depth"', tpl)
        self.assertNotIn("Administrator", tpl)
        self.assertNotIn("pg_password_enc", tpl)


if __name__ == "__main__":
    unittest.main()
