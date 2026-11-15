"""Product 1 — identity chrome + Owner Principal management UI."""

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
from app.services.identity_chrome import (
    hierarchy_identity,
    principal_capability_chips,
    resolve_staff_home,
)
from app.services.org_principals import (
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
)
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_lifecycle import (
    disable_level1_principal,
    enable_level1_principal,
)
from app.services.secret_box import encrypt_secret


ROOT = Path(__file__).resolve().parents[1]
_PLAIN_SECRET = "SecretA12!@xx"
_PG_ROLES = [
    {"id": 10, "name": "Operator", "is_owner": False},
    {"id": 2, "name": "Administrator", "is_owner": False},
    {"id": 1, "name": "OwnerRole", "is_owner": True},
]


def _owner_staff(owner: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": "admin",
            "web_owner": True,
            "username": "owner",
            "permissions": [],
            "pg_is_owner": True,
            "pg_permissions": ["pg_admins", "pg_users", "pg_overview"],
        },
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )


def _level1_staff(principal: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": "principal",
            "username": "l1web",
            "permissions": [],
            "pg_permissions": ["pg_users", "pg_nodes"],
            "pg_role_name": "Operator",
            "pg_is_owner": False,
            "web_owner": False,
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )


def _level2_staff(principal: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": "principal",
            "username": "l2web",
            "permissions": [],
            "pg_permissions": ["pg_users"],
            "pg_role_name": "Operator",
            "pg_is_owner": False,
            "web_owner": False,
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )


def _shop_reseller_staff(*, bot_user_id: int = 99) -> dict:
    return {
        "role": "reseller",
        "username": "shop",
        "bot_user_id": int(bot_user_id),
        "permissions": ["orders"],
        "org_depth": 1,
        "org_parent_id": 1,
        "org_principal_id": 8,
        "org_status": "active",
        "web_owner": False,
    }


class IdentityChromeTests(unittest.TestCase):
    def test_owner_label_is_malik_not_pg_role(self) -> None:
        ident = hierarchy_identity(
            {
                "role": "admin",
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
                "org_principal_id": 1,
                "pg_role_name": "Administrator",
            }
        )
        self.assertEqual(ident["kind"], "owner")
        self.assertEqual(ident["label"], "مالک")
        self.assertFalse(ident["show_pg_role"])
        self.assertIsNone(ident["pg_role_name"])
        self.assertTrue(ident["can_manage_representatives"])

    def test_hybrid_owner_without_admins_create_cannot_manage_reps(self) -> None:
        ident = hierarchy_identity(
            {
                "role": "admin",
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
                "org_principal_id": 1,
                "pg_is_owner": False,
                "pg_actions": {"admins": {"create": False}},
            }
        )
        self.assertEqual(ident["kind"], "owner")
        self.assertFalse(ident["can_manage_representatives"])
        self.assertFalse(ident["can_add_representative"])

    def test_l1_hierarchy_plus_live_pg_role(self) -> None:
        ident = hierarchy_identity(
            {
                "role": "principal",
                "org_depth": 1,
                "org_parent_id": 1,
                "org_status": "active",
                "org_principal_id": 2,
                "pg_role_name": "Operator",
                "pg_permissions": ["pg_users", "pg_nodes"],
            }
        )
        self.assertEqual(ident["kind"], "l1")
        self.assertEqual(ident["label"], "نماینده")
        self.assertTrue(ident["show_pg_role"])
        self.assertEqual(ident["pg_role_name"], "Operator")
        self.assertNotEqual(ident["label"], ident["pg_role_name"])
        self.assertIn("کاربران", ident["chips"])
        self.assertIn("نود", ident["chips"])

    def test_l2_hierarchy_plus_live_pg_role(self) -> None:
        ident = hierarchy_identity(
            {
                "role": "principal",
                "org_depth": 2,
                "org_parent_id": 2,
                "org_status": "active",
                "org_principal_id": 3,
                "pg_role_name": "Administrator",
                "pg_permissions": ["pg_hosts"],
            }
        )
        self.assertEqual(ident["kind"], "l2")
        self.assertEqual(ident["label"], "زیرنماینده")
        self.assertEqual(ident["pg_role_name"], "Administrator")
        self.assertNotEqual(ident["label"], "Administrator")

    def test_shop_reseller_keeps_commercial_label(self) -> None:
        ident = hierarchy_identity(_shop_reseller_staff())
        self.assertEqual(ident["kind"], "reseller")
        self.assertEqual(ident["label"], "نماینده")
        self.assertTrue(ident["commercial_reseller"])
        self.assertFalse(ident["show_pg_role"])

    def test_sticky_admin_without_owner_is_not_malik(self) -> None:
        ident = hierarchy_identity({"role": "admin", "username": "legacy"})
        self.assertEqual(ident["label"], "ادمین")
        self.assertEqual(ident["kind"], "admin_label")

    def test_chips_fail_closed_without_confirmed_permissions(self) -> None:
        self.assertEqual(
            principal_capability_chips(
                {"pg_role_name": "Administrator", "pg_permissions": None}
            ),
            [],
        )
        self.assertEqual(principal_capability_chips({"pg_role_name": "Operator"}), [])

    def test_chips_do_not_use_pg_role_name(self) -> None:
        chips = principal_capability_chips(
            {"pg_role_name": "Administrator", "pg_permissions": ["pg_users"]}
        )
        self.assertEqual(chips, ["کاربران"])
        self.assertNotIn("Administrator", chips)

    def test_owner_home_unchanged(self) -> None:
        dest = resolve_staff_home(
            {
                "role": "admin",
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
                "org_principal_id": 1,
            }
        )
        self.assertEqual(dest, ("template", "home.html"))

    def test_principal_web_l1_home_goes_to_pg(self) -> None:
        dest = resolve_staff_home(
            {
                "role": "principal",
                "org_depth": 1,
                "org_parent_id": 1,
                "org_status": "active",
                "org_principal_id": 2,
            }
        )
        self.assertEqual(dest, ("redirect", "/pg"))

    def test_principal_web_l2_home_goes_to_pg(self) -> None:
        dest = resolve_staff_home(
            {
                "role": "principal",
                "org_depth": 2,
                "org_parent_id": 2,
                "org_status": "active",
                "org_principal_id": 3,
            }
        )
        self.assertEqual(dest, ("redirect", "/pg"))

    def test_shop_l1_still_goes_to_reseller_home(self) -> None:
        dest = resolve_staff_home(_shop_reseller_staff())
        self.assertEqual(dest, ("template", "reseller_home.html"))


class PrincipalPageSourceContracts(unittest.TestCase):
    def test_routes_use_existing_owner_dependency(self) -> None:
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertIn("Depends(require_admin)", pages)
        self.assertIn("list_level1_principals", pages)
        self.assertIn("get_level1_principal_detail", pages)
        self.assertIn("disable_level1_principal", pages)
        self.assertIn("enable_level1_principal", pages)
        self.assertIn("provision_level1_principal", pages)
        self.assertNotIn("can_shop", pages)

    def test_app_registers_with_same_require_admin(self) -> None:
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("register_principal_pages", src)
        self.assertIn(
            "require_staff=require_staff",
            src,
        )

    def test_template_never_hardcodes_pg_roles_as_hierarchy(self) -> None:
        tpl = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertIn("نمایندگان", tpl)
        self.assertIn("/resellers/create-child", tpl)
        self.assertNotIn("Administrator", tpl)
        self.assertNotIn("Operator", tpl)
        self.assertNotIn("pg_password_enc", tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="org_principal_id"', tpl)
        self.assertNotIn('name="depth"', tpl)

    def test_nav_owner_only_and_not_reseller_label(self) -> None:
        from tests.sidebar_view import owner_sidebar, sidebar

        shop = sidebar("reseller", perms=("dashboard",), add_rep=True)["shop"]
        self.assertIn(("نمایندگان من", "/resellers"), shop)
        self.assertNotIn("نمایندگان من", [label for label, _ in sidebar("reseller", perms=("dashboard",))["shop"]])
        every_href = [h for nav in (shop, *owner_sidebar().values()) for _, h in nav]
        self.assertNotIn("/principals", every_href)

    def test_identity_chrome_labels_in_footer(self) -> None:
        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("مالک", base)
        self.assertIn("نقش پاسارگارد:", base)
        self.assertIn("role-tag-owner", base)
        self.assertIn("role-tag-principal", base)

    def test_home_pages_use_presentation_router(self) -> None:
        src = (ROOT / "app/api/home_pages.py").read_text(encoding="utf-8")
        self.assertIn("resolve_staff_home", src)


class PrincipalManagementHttpTests(unittest.IsolatedAsyncioTestCase):
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
            new=AsyncMock(return_value=(["pg_users", "pg_nodes"], {"name": "Operator", "id": 10})),
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
            active = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_a",
                pg_password_enc=encrypt_secret(_PLAIN_SECRET),
            )
            disabled = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_off",
                pg_password_enc=encrypt_secret(_PLAIN_SECRET),
                status="disabled",
            )
            l2 = await create_principal(
                session,
                parent_id=int(active.id),
                depth=2,
                pg_username="prin_l2",
            )
            session.add(
                OrgPrincipalProvision(
                    idempotency_key="seed-a",
                    principal_id=int(active.id),
                    pg_username="prin_a",
                    pg_role_id=10,
                    created_by_principal_id=int(owner.id),
                    status="completed",
                )
            )
            await session.commit()
            return owner, active, disabled, l2

    def _app_for(self, staff: dict) -> FastAPI:
        from app.api.app import render
        from app.api.principal_pages import register_principal_pages

        app = FastAPI()

        async def require_admin():
            if staff.get("role") != "admin" or not is_explicit_owner_staff(staff):
                raise HTTPException(status_code=403, detail="forbidden")
            return staff

        async def get_db():
            async with self.Session() as session:
                yield session

        register_principal_pages(
            app, render=render, require_admin=require_admin, get_db=get_db
        )
        return app

    def _client(self, staff: dict):
        transport = httpx.ASGITransport(app=self._app_for(staff))
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    async def test_owner_can_open_and_list(self) -> None:
        owner, active, disabled, _ = await self._seed()
        async with self._client(_owner_staff(owner)) as client:
            resp = await client.get("/principals", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location") or ""
        self.assertTrue(loc.startswith("/resellers"))
        self.assertNotIn(_PLAIN_SECRET, loc)
        self.assertNotIn("pg_password_enc", loc)
        _ = (active, disabled)

    async def test_disabled_principal_renders_disabled_state(self) -> None:
        owner, _, disabled, _ = await self._seed()
        async with self._client(_owner_staff(owner)) as client:
            resp = await client.get(
                f"/principals?detail={int(disabled.id)}", follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location") or ""
        self.assertIn("/resellers", loc)
        self.assertIn(f"detail={int(disabled.id)}", loc)
        self.assertNotIn(_PLAIN_SECRET, loc)

    async def test_l1_cannot_open(self) -> None:
        owner, active, _, _ = await self._seed()
        async with self._client(_level1_staff(active)) as client:
            resp = await client.get("/principals")
        self.assertEqual(resp.status_code, 403)

    async def test_l2_cannot_open(self) -> None:
        _, _, _, l2 = await self._seed()
        async with self._client(_level2_staff(l2)) as client:
            resp = await client.get("/principals")
        self.assertEqual(resp.status_code, 403)

    async def test_shop_reseller_cannot_open(self) -> None:
        await self._seed()
        async with self._client(_shop_reseller_staff()) as client:
            resp = await client.get("/principals")
        self.assertEqual(resp.status_code, 403)

    async def test_disable_calls_existing_service(self) -> None:
        owner, active, _, _ = await self._seed()
        staff = _owner_staff(owner)
        with patch(
            "app.api.principal_pages.disable_level1_principal",
            new_callable=AsyncMock,
        ) as wrapped:
            wrapped.side_effect = disable_level1_principal
            async with self._client(staff) as client:
                resp = await client.post(
                    f"/principals/{int(active.id)}/disable",
                    follow_redirects=False,
                )
            self.assertEqual(resp.status_code, 303)
            self.assertTrue(wrapped.await_count)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(active.id))
            self.assertEqual(row.status, "disabled")

    async def test_enable_calls_existing_service(self) -> None:
        owner, _, disabled, _ = await self._seed()
        staff = _owner_staff(owner)
        with patch(
            "app.api.principal_pages.enable_level1_principal",
            new_callable=AsyncMock,
        ) as wrapped:
            wrapped.side_effect = enable_level1_principal
            async with self._client(staff) as client:
                resp = await client.post(
                    f"/principals/{int(disabled.id)}/enable",
                    follow_redirects=False,
                )
            self.assertEqual(resp.status_code, 303)
            self.assertTrue(wrapped.await_count)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(disabled.id))
            self.assertEqual(row.status, "active")


class SimplePg:
    async def get_admin_roles(self):
        return list(_PG_ROLES)


class HomeRoutingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def _hit_home(self, staff: dict):
        from app.api.home_pages import register_home_pages
        from fastapi.responses import HTMLResponse

        captured: dict[str, str] = {}

        def render(request, name, context=None, status_code=200):
            captured["name"] = name
            return HTMLResponse(name, status_code=status_code)

        app = FastAPI()

        async def require_staff():
            return staff

        async def require_admin():
            raise HTTPException(status_code=403)

        async def get_db():
            yield None

        register_home_pages(
            app,
            render=render,
            require_admin=require_admin,
            require_staff=require_staff,
            get_db=get_db,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/home", follow_redirects=False)
        return resp, captured

    async def test_principal_l1_home_redirects_pg(self) -> None:
        resp, _ = await self._hit_home(
            {
                "role": "principal",
                "org_depth": 1,
                "org_parent_id": 1,
                "org_status": "active",
                "org_principal_id": 2,
                "username": "l1",
            }
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location"), "/pg")

    async def test_principal_l2_home_redirects_pg(self) -> None:
        resp, _ = await self._hit_home(
            {
                "role": "principal",
                "org_depth": 2,
                "org_parent_id": 2,
                "org_status": "active",
                "org_principal_id": 3,
                "username": "l2",
            }
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location"), "/pg")


if __name__ == "__main__":
    unittest.main()
