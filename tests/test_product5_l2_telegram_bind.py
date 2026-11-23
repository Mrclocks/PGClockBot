"""Product 5 — L2 Telegram bind/unbind UI wrapping bind_l2_bot_telegram()."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, OrgPrincipalWebIdentity, ResellerProfile, Role
from app.services.bot_l2_bind import bind_l2_bot_telegram, unbind_l2_bot_telegram
from app.services.bot_principal_identity import resolve_bot_org_principal
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
    attach_level1_web_identity,
    attach_level2_web_identity,
)
from app.services.secret_box import decrypt_secret, encrypt_secret
from tests.postgres_support import scoped_async_engine


ROOT = Path(__file__).resolve().parents[1]
_PG_PLAIN = "SecretA12!@xx"
_WEB_PLAIN = "WebL2login12!@"
_ADMIN_TID = 99001
_L2_TID = 99101
_SIB_TID = 99102
_FOREIGN_TID = 99121
_SHOP_TID = 99201
_PG_ROLES = [
    {"id": 10, "name": "Operator", "is_owner": False},
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


def _principal_staff(principal: OrgPrincipal, identity: OrgPrincipalWebIdentity) -> dict:
    return attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": identity.web_username,
            "web_identity_id": int(identity.id),
            "permissions": [],
            "pg_permissions": ["pg_users"],
            "pg_is_owner": False,
            "web_owner": False,
            "pg_can_create_admin": True,
            "pg_actions": {"admins": {"create": True}},
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )


class SimplePg:
    async def get_admin_roles(self):
        return list(_PG_ROLES)


class Product5L2TelegramBindHttpTests(unittest.IsolatedAsyncioTestCase):
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
            new=AsyncMock(return_value=(["pg_users"], {"name": "Operator", "id": 10})),
        )
        self._admins = patch(
            "app.services.bot_l2_bind._admin_ids_set",
            return_value=frozenset({_ADMIN_TID}),
        )
        self._pg.start()
        self._features.start()
        self._admins.start()

    async def asyncTearDown(self) -> None:
        self._admins.stop()
        self._features.stop()
        self._pg.stop()
        await self.engine.dispose()

    async def _bot_user(self, session, *, tid: int, code: str, role: str = Role.USER.value):
        user = BotUser(telegram_id=int(tid), role=role, referral_code=code)
        session.add(user)
        await session.flush()
        return user

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
            ident_a = await attach_level1_web_identity(
                session, principal_id=int(a.id), web_username="prin_a", password=_WEB_PLAIN
            )
            ident_a1 = await attach_level2_web_identity(
                session,
                principal_id=int(a1.id),
                web_username="child_a1",
                password=_WEB_PLAIN,
            )
            admin_u = await self._bot_user(
                session, tid=_ADMIN_TID, code="adm5", role=Role.ADMIN.value
            )
            l2_u = await self._bot_user(session, tid=_L2_TID, code="l2a1")
            sib_u = await self._bot_user(session, tid=_SIB_TID, code="l2a2")
            foreign_u = await self._bot_user(session, tid=_FOREIGN_TID, code="l2b1")
            await session.commit()
            return {
                "owner": owner,
                "a": a,
                "b": b,
                "a1": a1,
                "a2": a2,
                "b1": b1,
                "ident_a": ident_a,
                "ident_a1": ident_a1,
                "admin_u": admin_u,
                "l2_u": l2_u,
                "sib_u": sib_u,
                "foreign_u": foreign_u,
            }

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

    def _form(self, telegram_id: int, extra: dict | None = None) -> dict:
        data = {"telegram_id": str(int(telegram_id))}
        if extra:
            data.update(extra)
        return data

    async def _resolve_l2(self, session, user: BotUser, *, shop: bool = False):
        with patch(
            "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.config.get_settings",
            return_value=type("S", (), {"admin_ids": {_ADMIN_TID}})(),
        ):
            return await resolve_bot_org_principal(
                session,
                db_user=user,
                is_reseller_bot=shop,
            )

    async def test_owner_binds_descendant_l2(self) -> None:
        fx = await self._seed()
        owner, a1, l2_u = fx["owner"], fx["a1"], fx["l2_u"]
        async with self.Session() as session:
            before = await session.get(OrgPrincipal, int(a1.id))
            pg_user = before.pg_username
            pg_enc = before.pg_password_enc
            parent_id = before.parent_id
            depth = before.depth
        staff = _owner_staff(owner)
        async with self._client(staff) as client:
            page = await client.get(
                f"/principals?detail={int(a1.id)}", follow_redirects=False
            )
        self.assertEqual(page.status_code, 303)
        with patch(
            "app.api.principal_pages.bind_l2_bot_telegram",
            wraps=bind_l2_bot_telegram,
        ) as spy:
            async with self._client(staff) as client:
                resp = await client.post(
                    f"/principals/{int(a1.id)}/telegram-bind",
                    data=self._form(int(l2_u.telegram_id)),
                    follow_redirects=False,
                )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(spy.await_count)
        loc = resp.headers.get("location") or ""
        self.assertIn(f"detail={int(a1.id)}", loc)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a1.id))
            self.assertEqual(int(row.bot_user_id), int(l2_u.id))
            self.assertEqual(row.pg_username, pg_user)
            self.assertEqual(row.pg_password_enc, pg_enc)
            self.assertEqual(decrypt_secret(row.pg_password_enc), _PG_PLAIN)
            self.assertEqual(int(row.parent_id), int(parent_id))
            self.assertEqual(int(row.depth), int(depth))
            self.assertIsNone(row.reseller_profile_id)
            shops = await session.scalar(select(func.count()).select_from(ResellerProfile))
            self.assertEqual(int(shops or 0), 0)
            webs = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipalWebIdentity)
                .where(OrgPrincipalWebIdentity.principal_id == int(a1.id))
            )
            self.assertEqual(int(webs or 0), 1)
            user = await session.get(BotUser, int(l2_u.id))
            got = await self._resolve_l2(session, user)
            self.assertIsNone(got)
            shop_got = await self._resolve_l2(session, user, shop=True)
            self.assertNotEqual(getattr(shop_got, "id", None), int(a1.id))
            visible = await visible_principal_ids(session, row)
            self.assertEqual(visible, frozenset({int(a1.id)}))

    async def test_l1_binds_own_direct_l2(self) -> None:
        fx = await self._seed()
        staff = _principal_staff(fx["a"], fx["ident_a"])
        async with self._client(staff) as client:
            page = await client.get(
                f"/principals?detail={int(fx['a1'].id)}", follow_redirects=False
            )
            resp = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertEqual(resp.status_code, 303)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(fx["a1"].id))
            self.assertEqual(int(row.bot_user_id), int(fx["l2_u"].id))
            self.assertEqual(int(row.parent_id), int(fx["a"].id))

    async def test_l1_cannot_bind_sibling_or_foreign(self) -> None:
        fx = await self._seed()
        staff = _principal_staff(fx["a"], fx["ident_a"])
        async with self._client(staff) as client:
            foreign = await client.post(
                f"/principals/{int(fx['b1'].id)}/telegram-bind",
                data=self._form(int(fx["foreign_u"].telegram_id)),
                follow_redirects=False,
            )
            sibling_l1 = await client.post(
                f"/principals/{int(fx['a'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(sibling_l1.status_code, 403)
        async with self.Session() as session:
            b1 = await session.get(OrgPrincipal, int(fx["b1"].id))
            a = await session.get(OrgPrincipal, int(fx["a"].id))
            a1 = await session.get(OrgPrincipal, int(fx["a1"].id))
            self.assertIsNone(b1.bot_user_id)
            self.assertIsNone(a.bot_user_id)
            self.assertIsNone(a1.bot_user_id)

    async def test_l2_cannot_bind_or_unbind(self) -> None:
        fx = await self._seed()
        staff = _principal_staff(fx["a1"], fx["ident_a1"])
        async with self._client(staff) as client:
            listed = await client.get("/principals")
            bind = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(listed.status_code, 403)
        self.assertEqual(bind.status_code, 403)
        async with self.Session() as session:
            await bind_l2_bot_telegram(
                session,
                staff=_owner_staff(fx["owner"]),
                target_principal_id=int(fx["a1"].id),
                telegram_id=int(fx["l2_u"].telegram_id),
            )
            await session.commit()
        async with self._client(staff) as client:
            unbind = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-unbind",
                follow_redirects=False,
            )
        self.assertEqual(unbind.status_code, 403)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(fx["a1"].id))
            self.assertEqual(int(row.bot_user_id), int(fx["l2_u"].id))

    async def test_disabled_l2_denied(self) -> None:
        fx = await self._seed()
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(fx["a1"].id))
            row.status = "disabled"
            await session.commit()
        async with self._client(_owner_staff(fx["owner"])) as client:
            page = await client.get(
                f"/principals?detail={int(fx['a1'].id)}", follow_redirects=False
            )
            resp = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertEqual(resp.status_code, 403)

    async def test_disabled_parent_denied(self) -> None:
        fx = await self._seed()
        async with self.Session() as session:
            await disable_level1_principal(
                session, _owner_staff(fx["owner"]), int(fx["a"].id)
            )
            await session.commit()
        async with self._client(_owner_staff(fx["owner"])) as client:
            page = await client.get(
                f"/principals?detail={int(fx['a1'].id)}", follow_redirects=False
            )
            resp = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(page.status_code, 303)
        self.assertEqual(resp.status_code, 403)

    async def test_admin_ids_telegram_denied(self) -> None:
        fx = await self._seed()
        async with self._client(_owner_staff(fx["owner"])) as client:
            resp = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(_ADMIN_TID),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 403)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(fx["a1"].id))
            self.assertIsNone(row.bot_user_id)

    async def test_active_reseller_profile_denied(self) -> None:
        fx = await self._seed()
        async with self.Session() as session:
            shop_u = await self._bot_user(session, tid=_SHOP_TID, code="shop5")
            session.add(
                ResellerProfile(
                    user_id=int(shop_u.id),
                    is_active=True,
                    web_username="shop5",
                    web_password_hash="x" * 24,
                    setup_completed_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
            shop_tid = int(shop_u.telegram_id)
        async with self._client(_owner_staff(fx["owner"])) as client:
            resp = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(shop_tid),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 403)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(fx["a1"].id))
            self.assertIsNone(row.bot_user_id)

    async def test_telegram_already_bound_to_other_principal_denied(self) -> None:
        fx = await self._seed()
        owner = _owner_staff(fx["owner"])
        async with self._client(owner) as client:
            first = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
            second = await client.post(
                f"/principals/{int(fx['a2'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 403)
        async with self.Session() as session:
            a2 = await session.get(OrgPrincipal, int(fx["a2"].id))
            self.assertIsNone(a2.bot_user_id)

    async def test_target_already_bound_to_other_telegram_denied(self) -> None:
        fx = await self._seed()
        owner = _owner_staff(fx["owner"])
        async with self._client(owner) as client:
            first = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
            second = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["sib_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 403)
        async with self.Session() as session:
            a1 = await session.get(OrgPrincipal, int(fx["a1"].id))
            self.assertEqual(int(a1.bot_user_id), int(fx["l2_u"].id))

    async def test_duplicate_bot_user_id_denied(self) -> None:
        fx = await self._seed()
        async with self.Session() as session:
            a1 = await session.get(OrgPrincipal, int(fx["a1"].id))
            a2 = await session.get(OrgPrincipal, int(fx["a2"].id))
            a1.bot_user_id = int(fx["l2_u"].id)
            a2.bot_user_id = int(fx["l2_u"].id)
            await session.commit()
        async with self._client(_owner_staff(fx["owner"])) as client:
            resp = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 403)

    async def test_client_identity_hierarchy_tampering_denied(self) -> None:
        fx = await self._seed()
        staff = _principal_staff(fx["a"], fx["ident_a"])
        async with self._client(staff) as client:
            ignored = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(
                    int(fx["l2_u"].telegram_id),
                    extra={
                        "principal_id": str(int(fx["b1"].id)),
                        "parent_id": str(int(fx["b"].id)),
                        "depth": "1",
                        "org_principal_id": str(int(fx["b"].id)),
                        "web_owner": "1",
                        "bot_user_id": str(int(fx["foreign_u"].id)),
                        "role": "admin",
                    },
                ),
                follow_redirects=False,
            )
            wrong_path = await client.post(
                f"/principals/{int(fx['b1'].id)}/telegram-bind",
                data=self._form(int(fx["foreign_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(ignored.status_code, 303)
        self.assertEqual(wrong_path.status_code, 403)
        async with self.Session() as session:
            a1 = await session.get(OrgPrincipal, int(fx["a1"].id))
            b1 = await session.get(OrgPrincipal, int(fx["b1"].id))
            self.assertEqual(int(a1.bot_user_id), int(fx["l2_u"].id))
            self.assertNotEqual(int(a1.bot_user_id), int(fx["foreign_u"].id))
            self.assertIsNone(b1.bot_user_id)
            self.assertEqual(int(a1.depth), 2)
            self.assertEqual(int(a1.parent_id), int(fx["a"].id))

    async def test_unbind_clears_only_l2_binding(self) -> None:
        fx = await self._seed()
        owner = _owner_staff(fx["owner"])
        async with self._client(owner) as client:
            bind = await client.post(
                f"/principals/{int(fx['a1'].id)}/telegram-bind",
                data=self._form(int(fx["l2_u"].telegram_id)),
                follow_redirects=False,
            )
        self.assertEqual(bind.status_code, 303)
        async with self.Session() as session:
            before = await session.get(OrgPrincipal, int(fx["a1"].id))
            pg_enc = before.pg_password_enc
            parent_id = before.parent_id
            depth = before.depth
            web = (
                await session.execute(
                    select(OrgPrincipalWebIdentity).where(
                        OrgPrincipalWebIdentity.principal_id == int(fx["a1"].id)
                    )
                )
            ).scalar_one()
            web_hash = web.web_password_hash
        with patch(
            "app.api.principal_pages.unbind_l2_bot_telegram",
            wraps=unbind_l2_bot_telegram,
        ) as spy:
            async with self._client(owner) as client:
                unbind = await client.post(
                    f"/principals/{int(fx['a1'].id)}/telegram-unbind",
                    follow_redirects=False,
                )
        self.assertEqual(unbind.status_code, 303)
        self.assertTrue(spy.await_count)
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(fx["a1"].id))
            user = await session.get(BotUser, int(fx["l2_u"].id))
            self.assertIsNone(row.bot_user_id)
            self.assertIsNotNone(user)
            self.assertEqual(int(user.telegram_id), _L2_TID)
            self.assertEqual(row.pg_password_enc, pg_enc)
            self.assertEqual(int(row.parent_id), int(parent_id))
            self.assertEqual(int(row.depth), int(depth))
            web = (
                await session.execute(
                    select(OrgPrincipalWebIdentity).where(
                        OrgPrincipalWebIdentity.principal_id == int(fx["a1"].id)
                    )
                )
            ).scalar_one()
            self.assertEqual(web.web_password_hash, web_hash)
            got = await self._resolve_l2(session, user)
            self.assertIsNone(got)
            visible = await visible_principal_ids(session, row)
            self.assertEqual(visible, frozenset({int(row.id)}))


class Product5SourceContracts(unittest.TestCase):
    def test_wraps_existing_bind_service_only(self) -> None:
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertIn("bind_l2_bot_telegram", pages)
        self.assertIn("unbind_l2_bot_telegram", pages)
        self.assertIn("/telegram-bind", pages)
        self.assertIn("/telegram-unbind", pages)
        self.assertNotIn("target.bot_user_id", pages)
        self.assertNotIn("BotUser.role", pages)
        self.assertNotIn("bot_token", pages)
        svc = (ROOT / "app/services/bot_l2_bind.py").read_text(encoding="utf-8")
        self.assertIn("target.bot_user_id = None", svc)
        self.assertNotIn("session.delete", svc)
        tpl = (ROOT / "app/web/templates/principals.html").read_text(encoding="utf-8")
        self.assertIn("اتصال تلگرام", tpl)
        self.assertIn("قطع اتصال", tpl)
        self.assertIn('name="telegram_id"', tpl)
        self.assertNotIn('name="bot_user_id"', tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="depth"', tpl)


if __name__ == "__main__":
    unittest.main()
