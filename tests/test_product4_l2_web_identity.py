"""Product 4 — L2 Web identity UI wrapping attach_level2_web_identity()."""

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
from app.db.models import OrgPrincipal, OrgPrincipalProvision, OrgPrincipalWebIdentity
from app.services.org_principals import (
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_lifecycle import disable_level1_principal
from app.services.principal_web_identity import (
    ROLE_PRINCIPAL,
    attach_level2_web_identity,
    authenticate_level1_web,
    build_principal_session_payload,
    resolve_principal_web_session,
    staff_uses_owner_pg_credentials,
)
from app.services.secret_box import decrypt_secret, encrypt_secret
from tests.postgres_support import scoped_async_engine


ROOT = Path(__file__).resolve().parents[1]
_PG_PLAIN = "SecretA12!@xx"
_WEB_PLAIN = "WebL2login12!@"
_OWNER_ENV_PG = "env_owner_pg"
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
            "pg_can_create_admin": True,
        },
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )


def _l1_staff(principal: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"l1_{principal.pg_username}",
            "permissions": [],
            "pg_permissions": ["pg_users"],
            "pg_role_name": "Operator",
            "pg_is_owner": False,
            "web_owner": False,
            "pg_can_create_admin": True,
            "pg_actions": {"admins": {"create": True}},
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )


def _l2_staff(principal: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
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


class SimplePg:
    async def get_admin_roles(self):
        return list(_PG_ROLES)


class Product4L2WebIdentityHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._pg = patch("app.services.pasarguard.get_pg", return_value=SimplePg())
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
                pg_password_enc=encrypt_secret(_PG_PLAIN),
            )
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="prin_b",
                pg_password_enc=encrypt_secret(_PG_PLAIN),
            )
            a1 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="child_a1",
                pg_password_enc=encrypt_secret(_PG_PLAIN),
            )
            a2 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="child_a2",
                pg_password_enc=encrypt_secret(_PG_PLAIN),
            )
            b1 = await create_principal(
                session,
                parent_id=int(b.id),
                depth=2,
                pg_username="child_b1",
                pg_password_enc=encrypt_secret(_PG_PLAIN),
            )
            session.add(
                OrgPrincipalProvision(
                    idempotency_key="l2-a1-p4",
                    principal_id=int(a1.id),
                    pg_username="child_a1",
                    pg_role_id=10,
                    created_by_principal_id=int(a.id),
                    status="completed",
                )
            )
            await session.commit()
            return owner, a, b, a1, a2, b1

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

    def _form(self, username: str = "webl2a1", extra: dict | None = None) -> dict:
        data = {"username": username, "password": _WEB_PLAIN}
        if extra:
            data.update(extra)
        return data

    async def _identity_count(self, principal_id: int) -> int:
        async with self.Session() as session:
            n = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipalWebIdentity)
                .where(OrgPrincipalWebIdentity.principal_id == int(principal_id))
            )
        return int(n or 0)

    async def _resolve_login(self, session, username: str, password: str):
        auth = await authenticate_level1_web(
            session, username=username, password=password
        )
        self.assertIsNotNone(auth)
        with patch(
            "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.services.principal_web_identity.owner_env_pg_username",
            return_value=_OWNER_ENV_PG,
        ):
            staff = await resolve_principal_web_session(
                session, build_principal_session_payload(auth)
            )
        return auth, staff

    async def test_owner_attaches_l2_web_identity(self) -> None:
        owner, _, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            before = await session.get(OrgPrincipal, int(a1.id))
            pg_user = before.pg_username
            pg_enc = before.pg_password_enc
            bot_id = before.bot_user_id
            shop_id = before.reseller_profile_id
        staff = _owner_staff(owner)
        async with self._client(staff) as client:
            page = await client.get(
                f"/principals?detail={int(a1.id)}", follow_redirects=False
            )
        self.assertEqual(page.status_code, 303)
        self.assertTrue((page.headers.get("location") or "").startswith("/resellers"))
        with patch(
            "app.api.principal_pages.attach_level2_web_identity",
            wraps=attach_level2_web_identity,
        ) as spy:
            async with self._client(staff) as client:
                resp = await client.post(
                    f"/principals/{int(a1.id)}/web-identity",
                    data=self._form("webl2_owner"),
                    follow_redirects=False,
                )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(spy.await_count)
        loc = resp.headers.get("location") or ""
        self.assertIn(f"detail={int(a1.id)}", loc)
        self.assertNotIn(_WEB_PLAIN, loc)
        self.assertEqual(await self._identity_count(int(a1.id)), 1)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(row.pg_username, pg_user)
            self.assertEqual(row.pg_password_enc, pg_enc)
            self.assertEqual(decrypt_secret(row.pg_password_enc), _PG_PLAIN)
            self.assertEqual(row.bot_user_id, bot_id)
            self.assertEqual(row.reseller_profile_id, shop_id)
            ident = (
                await session.execute(
                    select(OrgPrincipalWebIdentity).where(
                        OrgPrincipalWebIdentity.principal_id == int(a1.id)
                    )
                )
            ).scalar_one()
            self.assertEqual(ident.web_username, pg_user)
            self.assertTrue(ident.is_active)
            self.assertNotEqual(ident.web_password_hash, _WEB_PLAIN)
            self.assertNotEqual(ident.web_password_hash, _PG_PLAIN)
            self.assertTrue(ident.web_password_hash.startswith("$2"))
            auth, resolved = await self._resolve_login(
                session, pg_user, _WEB_PLAIN
            )
            self.assertEqual(int(auth.principal.id), int(a1.id))
            self.assertEqual(int(auth.principal.depth), 2)
            self.assertEqual(int(resolved["org_principal_id"]), int(a1.id))
            self.assertEqual(int(resolved["org_depth"]), 2)
            visible = frozenset(int(x) for x in resolved["org_visible_principal_ids"])
            self.assertEqual(visible, frozenset({int(a1.id)}))
            live = await visible_principal_ids(session, row)
            self.assertEqual(live, frozenset({int(a1.id)}))
            self.assertFalse(staff_uses_owner_pg_credentials(resolved))
            self.assertNotEqual(resolved.get("pg_admin_username"), _OWNER_ENV_PG)

    async def test_l1_attaches_only_own_child(self) -> None:
        _, a, _, a1, _, _ = await self._seed()
        staff = _l1_staff(a)
        async with self._client(staff) as client:
            page = await client.get(
                f"/principals?detail={int(a1.id)}", follow_redirects=False
            )
            resp = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_own"),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(await self._identity_count(int(a1.id)), 1)

    async def test_l1_cannot_attach_sibling_or_foreign(self) -> None:
        _, a, _, a1, _, b1 = await self._seed()
        staff = _l1_staff(a)
        async with self._client(staff) as client:
            foreign = await client.post(
                f"/principals/{int(b1.id)}/web-identity",
                data=self._form("webl2_b1"),
                follow_redirects=False,
            )
            sibling_l1 = await client.post(
                f"/principals/{int(a.id)}/web-identity",
                data=self._form("webl2_l1"),
                follow_redirects=False,
            )
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(sibling_l1.status_code, 403)
        self.assertEqual(await self._identity_count(int(b1.id)), 0)
        self.assertEqual(await self._identity_count(int(a.id)), 0)
        self.assertEqual(await self._identity_count(int(a1.id)), 0)

    async def test_l2_denied(self) -> None:
        _, _, _, a1, _, _ = await self._seed()
        async with self._client(_l2_staff(a1)) as client:
            listed = await client.get("/principals")
            resp = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_self"),
                follow_redirects=False,
            )
        self.assertEqual(listed.status_code, 403)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._identity_count(int(a1.id)), 0)

    async def test_disabled_l2_denied(self) -> None:
        owner, _, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a1.id))
            row.status = "disabled"
            await session.commit()
        async with self._client(_owner_staff(owner)) as client:
            page = await client.get(
                f"/principals?detail={int(a1.id)}", follow_redirects=False
            )
            resp = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_off"),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._identity_count(int(a1.id)), 0)

    async def test_disabled_parent_denied(self) -> None:
        owner, a, _, a1, _, _ = await self._seed()
        async with self.Session() as session:
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
        async with self._client(_owner_staff(owner)) as client:
            page = await client.get(
                f"/principals?detail={int(a1.id)}", follow_redirects=False
            )
            resp = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_parent_off"),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(await self._identity_count(int(a1.id)), 0)

    async def test_target_must_be_depth2_with_active_l1_parent(self) -> None:
        owner, a, _, a1, _, _ = await self._seed()
        staff = _owner_staff(owner)
        async with self._client(staff) as client:
            on_l1 = await client.post(
                f"/principals/{int(a.id)}/web-identity",
                data=self._form("webl2_on_l1"),
                follow_redirects=False,
            )
            on_owner = await client.post(
                f"/principals/{int(owner.id)}/web-identity",
                data=self._form("webl2_on_owner"),
                follow_redirects=False,
            )
        self.assertEqual(on_l1.status_code, 303)
        self.assertEqual(on_owner.status_code, 403)
        self.assertEqual(await self._identity_count(int(a.id)), 1)
        self.assertEqual(await self._identity_count(int(owner.id)), 0)
        async with self._client(staff) as client:
            ok = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_depth2"),
                follow_redirects=False,
            )
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(await self._identity_count(int(a1.id)), 1)

    async def test_duplicate_username_rejected(self) -> None:
        owner, _, _, a1, a2, _ = await self._seed()
        staff = _owner_staff(owner)
        async with self._client(staff) as client:
            first = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_dup"),
                follow_redirects=False,
            )
            second = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form("webl2_dup"),
                follow_redirects=False,
            )
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        loc = second.headers.get("location") or ""
        self.assertIn("err=", loc)
        self.assertEqual(await self._identity_count(int(a1.id)), 1)
        self.assertEqual(await self._identity_count(int(a2.id)), 0)

    async def test_client_principal_parent_depth_tampering_denied(self) -> None:
        _, a, b, a1, _, b1 = await self._seed()
        staff = _l1_staff(a)
        async with self._client(staff) as client:
            ignored = await client.post(
                f"/principals/{int(a1.id)}/web-identity",
                data=self._form(
                    "webl2_tamp",
                    extra={
                        "principal_id": str(int(b1.id)),
                        "parent_id": str(int(b.id)),
                        "depth": "1",
                        "org_principal_id": str(int(b.id)),
                        "role": "admin",
                        "pg_username": "stolen",
                        "pg_password": _PG_PLAIN,
                    },
                ),
                follow_redirects=False,
            )
            wrong_path = await client.post(
                f"/principals/{int(b1.id)}/web-identity",
                data=self._form("webl2_wrong_path"),
                follow_redirects=False,
            )
        self.assertEqual(ignored.status_code, 303)
        self.assertEqual(wrong_path.status_code, 403)
        self.assertEqual(await self._identity_count(int(a1.id)), 1)
        self.assertEqual(await self._identity_count(int(b1.id)), 0)
        async with self.Session() as session:
            ident = (
                await session.execute(
                    select(OrgPrincipalWebIdentity).where(
                        OrgPrincipalWebIdentity.principal_id == int(a1.id)
                    )
                )
            ).scalar_one()
            self.assertEqual(ident.web_username, "child_a1")
            child = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(int(child.depth), 2)
            self.assertEqual(int(child.parent_id), int(a.id))
            self.assertEqual(child.pg_username, "child_a1")
            self.assertEqual(decrypt_secret(child.pg_password_enc), _PG_PLAIN)


class Product4SourceContracts(unittest.TestCase):
    def test_wraps_existing_web_identity_service_only(self) -> None:
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertIn("attach_level2_web_identity", pages)
        self.assertIn("/principals/{principal_id}/web-identity", pages)
        self.assertNotIn("hash_password", pages)
        self.assertNotIn("bcrypt", pages)
        self.assertNotIn("authenticate_level1_web(", pages)
        self.assertNotIn("resolve_principal_web_session(", pages)
        self.assertNotIn("bind_telegram", pages)
        self.assertNotIn("can_shop", pages)
        tpl = (ROOT / "app/web/templates/principals.html").read_text(encoding="utf-8")
        self.assertIn("فعال‌سازی ورود وب", tpl)
        self.assertIn('name="username"', tpl)
        self.assertIn('name="password"', tpl)
        self.assertNotIn('name="principal_id"', tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="depth"', tpl)
        start = tpl.find('id="modal-principal-web-identity"')
        self.assertGreater(start, 0)
        chunk = tpl[start : tpl.find("{% endif %}", start)]
        self.assertIn('name="username"', chunk)
        self.assertIn('name="password"', chunk)
        self.assertIn("readonly", chunk)
        self.assertIn("detail.pg_username", chunk)
        self.assertNotIn('name="pg_username"', chunk)
        self.assertNotIn('name="pg_password"', chunk)
        self.assertNotIn("web_password_hash", chunk)
        self.assertNotIn("pg_password_enc", tpl)


if __name__ == "__main__":
    unittest.main()
