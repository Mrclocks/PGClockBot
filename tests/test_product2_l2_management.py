"""Product 2 — L2 Principal management UI + lifecycle."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal, OrgPrincipalProvision
from app.services.org_principals import (
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
)
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_l2_lifecycle import (
    Level2PrincipalView,
    disable_level2_principal,
    enable_level2_principal,
    get_level2_principal_detail,
    list_level2_principals,
)
from app.services.principal_lifecycle import (
    PrincipalLifecycleError,
    disable_level1_principal,
    public_views_contain_secret,
)
from app.services.secret_box import encrypt_secret


ROOT = Path(__file__).resolve().parents[1]
_PLAIN = "SecretA12!@xx"
_PG_ROLES = [
    {"id": 10, "name": "Operator", "is_owner": False},
    {"id": 2, "name": "Administrator", "is_owner": False},
]


def _owner_staff(owner: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": "admin",
            "web_owner": True,
            "username": "owner",
            "permissions": [],
            "pg_is_owner": True,
            "pg_permissions": ["pg_admins"],
        },
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )


def _l1_staff(principal: OrgPrincipal, *, capable: bool = False) -> dict:
    staff = attach_org_principal_fields(
        {
            "role": "principal",
            "username": f"l1_{principal.pg_username}",
            "permissions": [],
            "pg_permissions": ["pg_users"],
            "pg_role_name": "Operator",
            "pg_is_owner": False,
            "web_owner": False,
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )
    if capable:
        staff["pg_can_create_admin"] = True
        staff["pg_actions"] = {"admins": {"create": True}}
    else:
        staff["pg_can_create_admin"] = False
        staff["pg_actions"] = {"admins": {"create": False}}
    return staff


def _l2_staff(principal: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": "principal",
            "username": f"l2_{principal.pg_username}",
            "permissions": [],
            "pg_permissions": ["pg_users"],
            "pg_role_name": "Operator",
            "pg_is_owner": False,
            "web_owner": False,
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )


def _shop_staff() -> dict:
    return {
        "role": "reseller",
        "username": "shop",
        "bot_user_id": 99,
        "org_depth": 1,
        "org_parent_id": 1,
        "org_principal_id": 8,
        "org_status": "active",
        "web_owner": False,
    }


class SimplePg:
    async def get_admin_roles(self):
        return list(_PG_ROLES)


class L2LifecycleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._pg = patch(
            "app.services.pasarguard.get_pg",
            return_value=SimplePg(),
        )
        self._pg.start()

    async def asyncTearDown(self) -> None:
        self._pg.stop()
        await self.engine.dispose()

    async def _seed(self):
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_a",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_b",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            a1 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="child_a1",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            a2 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="child_a2",
                pg_password_enc=encrypt_secret(_PLAIN),
                status="disabled",
            )
            b1 = await create_principal(
                session,
                parent_id=int(b.id),
                depth=2,
                pg_username="child_b1",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            session.add(
                OrgPrincipalProvision(
                    idempotency_key="l2-a1",
                    principal_id=int(a1.id),
                    pg_username="child_a1",
                    pg_role_id=10,
                    created_by_principal_id=int(a.id),
                    status="completed",
                )
            )
            await session.commit()
            return owner, a, b, a1, a2, b1

    async def test_owner_lists_descendant_l2(self) -> None:
        owner, a, b, a1, a2, b1 = await self._seed()
        async with self.Session() as session:
            views = await list_level2_principals(session, _owner_staff(owner))
        ids = {v.principal_id for v in views}
        self.assertEqual(ids, {int(a1.id), int(a2.id), int(b1.id)})
        self.assertTrue(all(v.depth == 2 for v in views))
        self.assertTrue(all(v.parent_id in {int(a.id), int(b.id)} for v in views))
        self.assertFalse(public_views_contain_secret(views))
        blob = str([v.to_public_dict() for v in views])
        self.assertNotIn(_PLAIN, blob)
        self.assertNotIn("pg_password_enc", blob)

    async def test_l1_lists_only_direct_children(self) -> None:
        owner, a, b, a1, a2, b1 = await self._seed()
        async with self.Session() as session:
            views = await list_level2_principals(session, _l1_staff(a, capable=True))
        ids = {v.principal_id for v in views}
        self.assertEqual(ids, {int(a1.id), int(a2.id)})
        self.assertNotIn(int(b1.id), ids)
        self.assertNotIn(int(b.id), ids)
        self.assertNotIn(int(a.id), ids)

    async def test_l2_cannot_list(self) -> None:
        _, _, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await list_level2_principals(session, _l2_staff(a1))
        self.assertEqual(ctx.exception.code, "forbidden")

    async def test_shop_reseller_cannot_list(self) -> None:
        _owner, a, _, _, _, _ = await self._seed()
        staff = attach_org_principal_fields(
            {
                "role": "reseller",
                "username": "shop",
                "bot_user_id": 99,
                "permissions": ["orders"],
                "web_owner": False,
            },
            a,
            visible_principal_ids=frozenset({int(a.id)}),
        )
        async with self.Session() as session:
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await list_level2_principals(session, staff)
        self.assertEqual(ctx.exception.code, "pg_capability_denied")

    async def test_sibling_isolation_detail_and_disable(self) -> None:
        owner, a, _, _, _, b1 = await self._seed()
        async with self.Session() as session:
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await get_level2_principal_detail(session, _l1_staff(a, capable=True), int(b1.id))
            self.assertEqual(ctx.exception.code, "out_of_scope")
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await disable_level2_principal(session, _l1_staff(a, capable=True), int(b1.id))
            self.assertEqual(ctx.exception.code, "out_of_scope")
            row = await session.get(OrgPrincipal, int(b1.id))
            self.assertEqual(row.status, "active")

    async def test_tampered_parent_filter_denied(self) -> None:
        _, a, b, _, _, _ = await self._seed()
        async with self.Session() as session:
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await list_level2_principals(
                    session, _l1_staff(a, capable=True), parent_id=int(b.id)
                )
        self.assertEqual(ctx.exception.code, "out_of_scope")

    async def test_disable_enable_roundtrip(self) -> None:
        owner, a, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            off = await disable_level2_principal(session, _l1_staff(a, capable=True), int(a1.id))
            await session.commit()
            self.assertEqual(off.status, "disabled")
            self.assertEqual(off.depth, 2)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(row.status, "disabled")
            on = await enable_level2_principal(session, _owner_staff(owner), int(a1.id))
            await session.commit()
            self.assertEqual(on.status, "active")
            self.assertTrue(on.can_manage)

    async def test_inactive_parent_blocks_mutation(self) -> None:
        owner, a, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await disable_level2_principal(session, _owner_staff(owner), int(a1.id))
            self.assertEqual(ctx.exception.code, "parent_disabled")
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await enable_level2_principal(session, _owner_staff(owner), int(a1.id))
            self.assertEqual(ctx.exception.code, "parent_disabled")
            row = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(row.status, "active")
            detail = await get_level2_principal_detail(
                session, _owner_staff(owner), int(a1.id)
            )
            self.assertFalse(detail.can_manage)

    async def test_cannot_disable_owner_via_l2_path(self) -> None:
        owner, a, _, _, _, _ = await self._seed()
        async with self.Session() as session:
            with self.assertRaises(PrincipalLifecycleError):
                await disable_level2_principal(
                    session, _owner_staff(owner), int(owner.id)
                )
            with self.assertRaises(PrincipalLifecycleError):
                await disable_level2_principal(session, _l1_staff(a, capable=True), int(owner.id))

    async def test_pg_role_is_secondary_not_hierarchy(self) -> None:
        owner, _, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            detail = await get_level2_principal_detail(
                session, _owner_staff(owner), int(a1.id)
            )
        self.assertEqual(detail.depth, 2)
        self.assertNotEqual(detail.pg_role_name, "نماینده")
        self.assertNotEqual(detail.pg_role_name, "زیرمجموعه")
        data = detail.to_public_dict()
        self.assertNotIn("password", data)
        self.assertIsInstance(detail, Level2PrincipalView)


class L2ManagementHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._pg = patch(
            "app.services.pasarguard.get_pg",
            return_value=SimplePg(),
        )
        self._features = patch(
            "app.services.pg_access.resolve_reseller_pg_features",
            new=AsyncMock(
                return_value=(["pg_users"], {"name": "Operator", "id": 10})
            ),
        )
        self._pg.start()
        self._features.start()

    async def asyncTearDown(self) -> None:
        self._features.stop()
        self._pg.stop()
        await self.engine.dispose()

    async def _seed(self):
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_a",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_b",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            a1 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="child_a1",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            b1 = await create_principal(
                session,
                parent_id=int(b.id),
                depth=2,
                pg_username="child_b1",
                pg_password_enc=encrypt_secret(_PLAIN),
            )
            session.add(
                OrgPrincipalProvision(
                    idempotency_key="l2-a1",
                    principal_id=int(a1.id),
                    pg_username="child_a1",
                    pg_role_id=10,
                    created_by_principal_id=int(a.id),
                    status="completed",
                )
            )
            await session.commit()
            return owner, a, b, a1, b1

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

    async def test_owner_sees_l1_and_l2(self) -> None:
        owner, a, _, a1, b1 = await self._seed()
        async with self._client(_owner_staff(owner)) as client:
            resp = await client.get("/principals", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location") or ""
        self.assertTrue(loc.startswith("/resellers"))
        self.assertNotIn(_PLAIN, loc)
        _ = (a, a1, b1)

    async def test_l1_without_capability_cannot_open(self) -> None:
        _, a, _, a1, b1 = await self._seed()
        async with self._client(_l1_staff(a, capable=False)) as client:
            resp = await client.get("/principals", follow_redirects=False)
        self.assertEqual(resp.status_code, 403)
        _ = (a1, b1)

    async def test_l1_with_capability_redirects_to_resellers(self) -> None:
        _, a, _, _, _ = await self._seed()
        async with self._client(_l1_staff(a, capable=True)) as client:
            resp = await client.get("/principals", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue((resp.headers.get("location") or "").startswith("/resellers"))

    async def test_l2_cannot_open(self) -> None:
        _, _, _, a1, _ = await self._seed()
        async with self._client(_l2_staff(a1)) as client:
            resp = await client.get("/principals")
        self.assertEqual(resp.status_code, 403)

    async def test_shop_cannot_open(self) -> None:
        await self._seed()
        async with self._client(_shop_staff()) as client:
            resp = await client.get("/principals")
        self.assertEqual(resp.status_code, 403)

    async def test_l1_disable_enable_own_child(self) -> None:
        _, a, _, a1, _ = await self._seed()
        staff = _l1_staff(a, capable=True)
        async with self._client(staff) as client:
            resp = await client.post(
                f"/principals/{int(a1.id)}/disable", follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(row.status, "disabled")
        async with self._client(staff) as client:
            page = await client.get(
                f"/principals?detail={int(a1.id)}", follow_redirects=False
            )
        self.assertEqual(page.status_code, 303)
        async with self._client(staff) as client:
            resp = await client.post(
                f"/principals/{int(a1.id)}/enable", follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(row.status, "active")

    async def test_l1_cannot_disable_foreign_or_sibling_l1(self) -> None:
        _, a, b, _, b1 = await self._seed()
        staff = _l1_staff(a, capable=True)
        async with self._client(staff) as client:
            resp = await client.post(
                f"/principals/{int(b1.id)}/disable", follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(b1.id))
            self.assertEqual(row.status, "active")
        async with self._client(staff) as client:
            resp = await client.post(
                f"/principals/{int(b.id)}/disable", follow_redirects=False
            )
        self.assertIn(resp.status_code, {303, 403})
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(b.id))
            self.assertEqual(row.status, "active")

    async def test_l1_cannot_create_l1(self) -> None:
        _, a, _, _, _ = await self._seed()
        async with self._client(_l1_staff(a, capable=True)) as client:
            resp = await client.post(
                "/principals/create",
                data={
                    "pg_username": "evil",
                    "pg_password": _PLAIN,
                    "pg_role_id": "10",
                    "parent_id": "1",
                    "depth": "1",
                },
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 403)


class Product2SourceContracts(unittest.TestCase):
    def test_no_new_authz_or_bind_ui(self) -> None:
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertIn("list_level2_principals", pages)
        self.assertIn("disable_level2_principal", pages)
        self.assertIn("Depends(require_admin)", pages)
        self.assertNotIn("can_shop", pages)
        self.assertNotIn("bind_telegram", pages)
        tpl = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertIn("افزودن نماینده", tpl)
        self.assertIn("/resellers/create-child", tpl)
        self.assertNotIn("Administrator", tpl)
        self.assertNotIn("pg_password_enc", tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="depth"', tpl)
        from tests.sidebar_view import sidebar

        self.assertNotIn("نمایندگان من", [label for label, _ in sidebar("principal", perms=("dashboard",))["shop"]])

    def test_l2_nav_not_shown_for_l2_kind(self) -> None:
        from tests.sidebar_view import sidebar

        l2 = sidebar("principal", perms=("dashboard",), add_rep=False)["shop"]
        self.assertNotIn("/resellers", [h for _, h in l2])
        self.assertNotIn("/principals", [h for _, h in l2])


if __name__ == "__main__":
    unittest.main()
