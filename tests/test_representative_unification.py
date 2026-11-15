"""Representative unification — identity, hierarchy, shop package, isolation."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
from app.services.org_principals import (
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
)
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    Level2ProvisionRequest,
    child_uses_parent_or_owner_pg_credentials,
    provision_level2_child,
)
from app.services.principal_web_identity import ROLE_PRINCIPAL
from app.services.representative_unification import (
    descendant_shop_profile_ids,
    staff_can_manage_representatives,
    staff_is_sub_representative,
)
from app.services.secret_box import decrypt_secret, encrypt_secret


ROOT = Path(__file__).resolve().parents[1]
_PARENT_PASSWORD = "PpQq56!@GhIj"
_CHILD_PASSWORD = "CcDd34!@EfGh"
_OWNER_ENV_PG = "env_owner_pg"
_PARENT_PG_ROLES = [
    {"id": 10, "name": "Operator", "is_owner": False},
    {"id": 12, "name": "CustomRoleX", "is_owner": False},
    {"id": 1, "name": "Owner", "is_owner": True},
]


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


def _l1_staff(principal: OrgPrincipal, *, capable: bool = True) -> dict:
    staff = attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"web_{principal.pg_username or principal.id}",
            "web_identity_id": int(principal.id),
            "pg_admin_username": principal.pg_username,
            "pg_is_owner": False,
            "web_owner": False,
            "pg_permissions": ["pg_users"],
            "pg_role_name": "Administrator",
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )
    if capable:
        staff["pg_can_create_admin"] = True
        staff["pg_actions"] = {"admins": {"create": True}}
    else:
        staff["pg_can_create_admin"] = False
        staff["pg_actions"] = {"admins": {"create": False}, "users": {"create": True}}
        staff["pg_role"] = {
            "id": 99,
            "name": "Administrator",
            "is_owner": False,
            "permissions": {"users": {"create": True}},
        }
    return staff


def _l2_staff(child: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"web_{child.id}",
            "web_identity_id": int(child.id),
            "pg_can_create_admin": True,
            "pg_actions": {"admins": {"create": True}},
            "pg_role_name": "Administrator",
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
        self.modify_admin = AsyncMock(return_value=True)


class PlatformPg:
    def __init__(self) -> None:
        self.get_admin_roles = AsyncMock(return_value=list(_PARENT_PG_ROLES))
        self.get_admins = AsyncMock(return_value=[])
        self.get_admins_simple = AsyncMock(return_value=[])
        self.create_admin = AsyncMock()
        self.modify_admin = AsyncMock()


class RepresentativeUnificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._parent_pg = ParentPg()
        self._platform = PlatformPg()
        self._pg = patch(
            "app.services.pasarguard.get_pg", return_value=self._platform
        )
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
            await session.commit()
            return owner, a, b

    def _app_for(self, staff: dict) -> FastAPI:
        from app.api.app import render
        from app.api.principal_pages import register_principal_pages
        from app.api.reseller_pages import register_reseller_pages

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

        register_reseller_pages(
            app,
            render=render,
            require_admin=require_admin,
            require_staff=require_staff,
            get_db=get_db,
        )
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

    def test_capability_gates_not_role_name(self) -> None:
        owner = type("P", (), {"id": 1, "depth": 0, "parent_id": None, "status": "active"})()
        owner_staff = {
            "role": "admin",
            "web_owner": True,
            "pg_is_owner": True,
            "org_depth": 0,
            "org_principal_id": 1,
            "org_status": "active",
        }
        self.assertTrue(staff_can_manage_representatives(owner_staff))
        hybrid_no = {
            **owner_staff,
            "pg_is_owner": False,
            "pg_actions": {"admins": {"create": False}},
            "pg_role": {"permissions": {"users": {"create": True}}},
        }
        self.assertFalse(staff_can_manage_representatives(hybrid_no))
        hybrid_yes = {
            **owner_staff,
            "pg_is_owner": False,
            "pg_actions": {"admins": {"create": True}},
        }
        self.assertTrue(staff_can_manage_representatives(hybrid_yes))
        l1_cap = {
            "role": "principal",
            "org_depth": 1,
            "org_principal_id": 2,
            "org_status": "active",
            "pg_can_create_admin": True,
            "web_owner": False,
            "pg_is_owner": False,
        }
        self.assertTrue(staff_can_manage_representatives(l1_cap))
        l1_name_only = {
            "role": "principal",
            "org_depth": 1,
            "org_principal_id": 2,
            "org_status": "active",
            "pg_can_create_admin": False,
            "pg_actions": {"admins": {"create": False}},
            "pg_role_name": "Administrator",
            "pg_role": {
                "name": "Administrator",
                "permissions": {"users": {"create": True}},
            },
            "web_owner": False,
            "pg_is_owner": False,
        }
        self.assertFalse(staff_can_manage_representatives(l1_name_only))
        l2 = {
            "role": "principal",
            "org_depth": 2,
            "org_principal_id": 3,
            "org_status": "active",
            "pg_can_create_admin": True,
            "pg_actions": {"admins": {"create": True}},
            "web_owner": False,
            "pg_is_owner": False,
        }
        self.assertTrue(staff_is_sub_representative(l2))
        self.assertFalse(staff_can_manage_representatives(l2))
        _ = owner

    async def test_owner_to_representative_and_capable_l1_to_sub(self) -> None:
        owner, a, b = await self._seed()
        async with self.Session() as session:
            staff = _l1_staff(a)
            result = await provision_level2_child(
                session,
                staff,
                Level2ProvisionRequest(
                    pg_username="child_a1",
                    pg_password=_CHILD_PASSWORD,
                    pg_role_id=12,
                    idempotency_key="unify-a1",
                    parent_id=int(b.id),
                    depth=1,
                ),
            )
            await session.commit()
            child = result.principal
            parent = await session.get(OrgPrincipal, int(a.id))
            self.assertEqual(int(child.depth), 2)
            self.assertEqual(int(child.parent_id), int(a.id))
            self.assertNotEqual(int(child.parent_id), int(b.id))
            self.assertFalse(child_uses_parent_or_owner_pg_credentials(child, parent))
            self.assertEqual(decrypt_secret(child.pg_password_enc), _CHILD_PASSWORD)
            self.assertIsNotNone(child.reseller_profile_id)
            shop = await session.get(ResellerProfile, int(child.reseller_profile_id))
            self.assertEqual(shop.pg_admin_username, "child_a1")
            self.assertIsNone(shop.pg_admin_password_enc)
            self.assertIsNone(shop.web_username)
            self.assertEqual(int(child.bot_user_id), int(shop.user_id))
            user = await session.get(BotUser, int(shop.user_id))
            self.assertEqual(user.role, Role.RESELLER.value)
        self.assertTrue(self._parent_pg.create_admin.await_count)
        self.assertFalse(self._platform.create_admin.await_count)
        _ = owner

    async def test_l1_without_capability_denied(self) -> None:
        _, a, _ = await self._seed()
        async with self.Session() as session:
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    _l1_staff(a, capable=False),
                    Level2ProvisionRequest(
                        pg_username="denied_cap",
                        pg_password=_CHILD_PASSWORD,
                        pg_role_id=12,
                        idempotency_key="unify-deny-cap",
                    ),
                )
        self.assertEqual(ctx.exception.code, "pg_capability_denied")
        self.assertFalse(self._parent_pg.create_admin.await_count)

    async def test_sub_representative_cannot_create_even_with_capability(self) -> None:
        _, a, _ = await self._seed()
        async with self.Session() as session:
            child = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="existing_l2",
                pg_password_enc=encrypt_secret(_CHILD_PASSWORD),
            )
            await session.commit()
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    _l2_staff(child),
                    Level2ProvisionRequest(
                        pg_username="from_l2",
                        pg_password=_CHILD_PASSWORD,
                        pg_role_id=12,
                        idempotency_key="unify-l2",
                    ),
                )
        self.assertEqual(ctx.exception.code, "not_level1")
        self.assertFalse(self._parent_pg.create_admin.await_count)

    async def test_sibling_parent_foreign_shop_isolation(self) -> None:
        owner, a, b = await self._seed()
        async with self.Session() as session:
            ra = await provision_level2_child(
                session,
                _l1_staff(a),
                Level2ProvisionRequest(
                    pg_username="iso_a",
                    pg_password=_CHILD_PASSWORD,
                    pg_role_id=12,
                    idempotency_key="iso-a",
                ),
            )
            rb = await provision_level2_child(
                session,
                _l1_staff(b),
                Level2ProvisionRequest(
                    pg_username="iso_b",
                    pg_password=_CHILD_PASSWORD,
                    pg_role_id=12,
                    idempotency_key="iso-b",
                ),
            )
            await session.commit()
            ids_a = await descendant_shop_profile_ids(session, _l1_staff(a))
            ids_b = await descendant_shop_profile_ids(session, _l1_staff(b))
            self.assertEqual(ids_a, frozenset({int(ra.principal.reseller_profile_id)}))
            self.assertEqual(ids_b, frozenset({int(rb.principal.reseller_profile_id)}))
            self.assertTrue(ids_a.isdisjoint(ids_b))
            owner_ids = await descendant_shop_profile_ids(session, _owner_staff(owner))
            self.assertEqual(owner_ids, ids_a | ids_b)
            l2_ids = await descendant_shop_profile_ids(session, _l2_staff(ra.principal))
            self.assertEqual(l2_ids, frozenset())

    async def test_web_create_child_forged_ids_and_owner_only(self) -> None:
        owner, a, b = await self._seed()
        form = {
            "pg_username": "web_child",
            "pg_password": _CHILD_PASSWORD,
            "pg_role_id": "12",
            "parent_id": str(int(b.id)),
            "depth": "1",
            "principal_id": str(int(b.id)),
            "reseller_profile_id": "999",
        }
        async with self._client(_l1_staff(a)) as client:
            resp = await client.post(
                "/resellers/create-child", data=form, follow_redirects=False
            )
        self.assertEqual(resp.status_code, 303)
        loc = resp.headers.get("location") or ""
        self.assertTrue(loc.startswith("/resellers"))
        self.assertNotIn(_CHILD_PASSWORD, loc)
        self.assertNotIn(_PARENT_PASSWORD, loc)
        async with self.Session() as session:
            child = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.pg_username == "web_child")
                )
            ).scalar_one()
            self.assertEqual(int(child.parent_id), int(a.id))
            self.assertEqual(int(child.depth), 2)

        async with self._client(_l1_staff(a, capable=False)) as client:
            deny = await client.post(
                "/resellers/create-child", data=form, follow_redirects=False
            )
            listed = await client.get("/resellers", follow_redirects=False)
        self.assertEqual(deny.status_code, 403)
        self.assertEqual(listed.status_code, 403)

        async with self._client(_l2_staff(child)) as client:
            l2_create = await client.post(
                "/resellers/create-child", data=form, follow_redirects=False
            )
            l2_list = await client.get("/resellers", follow_redirects=False)
        self.assertEqual(l2_create.status_code, 403)
        self.assertEqual(l2_list.status_code, 403)

        async with self._client(_owner_staff(owner)) as client:
            owner_child = await client.post(
                "/resellers/create-child", data=form, follow_redirects=False
            )
            owner_list = await client.get("/resellers", follow_redirects=False)
            apps = await client.get("/resellers/applications", follow_redirects=False)
        self.assertEqual(owner_child.status_code, 403)
        self.assertEqual(owner_list.status_code, 200)
        self.assertIn("نمایندگان", owner_list.text)
        self.assertNotIn("Level 1", owner_list.text)
        self.assertNotIn("Principal", owner_list.text)
        self.assertNotIn(_CHILD_PASSWORD, owner_list.text)
        self.assertNotIn("pg_password_enc", owner_list.text)
        self.assertIn(apps.status_code, {200, 303})

        async with self._client(_l1_staff(a)) as client:
            apps_l1 = await client.get("/resellers/applications", follow_redirects=False)
            create_shop = await client.post("/resellers", data={"telegram_id": "1"})
        self.assertEqual(apps_l1.status_code, 403)
        self.assertEqual(create_shop.status_code, 403)

    async def test_disabled_parent_and_child_fail_closed(self) -> None:
        _, a, _ = await self._seed()
        async with self.Session() as session:
            row = await session.get(OrgPrincipal, int(a.id))
            row.status = "disabled"
            await session.commit()
            staff = _l1_staff(a)
            staff["org_status"] = "active"
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    staff,
                    Level2ProvisionRequest(
                        pg_username="from_disabled",
                        pg_password=_CHILD_PASSWORD,
                        pg_role_id=12,
                        idempotency_key="unify-dis",
                    ),
                )
        self.assertEqual(ctx.exception.code, "parent_disabled")

    async def test_web_bot_parity_and_no_secret_in_ui(self) -> None:
        _, a, _ = await self._seed()
        async with self._client(_l1_staff(a)) as client:
            page = await client.get("/resellers")
        self.assertEqual(page.status_code, 200)
        self.assertIn("نمایندگان من", page.text)
        self.assertIn("افزودن نماینده", page.text)
        self.assertIn("/resellers/create-child", page.text)
        self.assertNotIn("Level 1", page.text)
        self.assertNotIn("Level 2", page.text)
        self.assertNotIn("Principal", page.text)
        self.assertNotIn(_PARENT_PASSWORD, page.text)
        keyboards = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        reply_kb = (ROOT / "app/bot/reply_keyboards.py").read_text(encoding="utf-8")
        self.assertIn("REPLY_ACTION_RES_ADD_REP", keyboards)
        # The label now comes from settings; check the button the panel actually builds.
        from app.bot.keyboards import REPLY_ACTION_RES_ADD_REP  # loads reply_keyboards in order
        from app.bot.reply_keyboards import _reseller_submenu_entries
        from app.db.models import ResellerProfile

        entries = dict(
            _reseller_submenu_entries(
                ResellerProfile(id=1, user_id=2, is_active=True), can_add_representative=True
            )
        )
        self.assertIn("افزودن نماینده", entries[REPLY_ACTION_RES_ADD_REP])
        self.assertIn("can_add_representative", reply_kb)
        self.assertIn("REPLY_ACTION_RES_ADD_REP", reply_kb)
        handler = (ROOT / "app/bot/handlers/reseller_reps.py").read_text(encoding="utf-8")
        self.assertIn("provision_level2_child", handler)
        self.assertIn("assert_staff_can_manage_representatives", handler)
        self.assertIn("get_pg_for_principal", handler)
        self.assertNotIn("get_pg()", handler)

    async def test_child_pg_client_not_owner_get_pg(self) -> None:
        _, a, _ = await self._seed()
        from app.services.pasarguard import get_pg_for_principal, get_pg_for_reseller

        async with self.Session() as session:
            result = await provision_level2_child(
                session,
                _l1_staff(a),
                Level2ProvisionRequest(
                    pg_username="own_client",
                    pg_password=_CHILD_PASSWORD,
                    pg_role_id=12,
                    idempotency_key="unify-client",
                ),
            )
            await session.commit()
            child = result.principal
            fake = object()
            with patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake),
            ) as spy_principal, patch(
                "app.services.pasarguard.get_pg",
                return_value=self._platform,
            ) as spy_owner:
                client = await get_pg_for_reseller(session, int(child.bot_user_id))
            self.assertIs(client, fake)
            self.assertTrue(spy_principal.await_count)
            self.assertFalse(spy_owner.called)
        _ = get_pg_for_principal

    async def test_l2_never_uses_owner_bot(self) -> None:
        from app.services.bot_l2_bind import bind_l2_bot_telegram
        from app.services.bot_principal_identity import resolve_bot_org_principal
        from app.services.resellers import apply_reseller_panel_password

        owner, a, _ = await self._seed()
        async with self.Session() as session:
            leftover = BotUser(
                telegram_id=881001, role=Role.USER.value, referral_code="l2ownbot"
            )
            session.add(leftover)
            shop_less = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="legacy_l2",
                pg_password_enc=encrypt_secret(_CHILD_PASSWORD),
            )
            await session.flush()
            await bind_l2_bot_telegram(
                session,
                staff=_owner_staff(owner),
                target_principal_id=int(shop_less.id),
                telegram_id=int(leftover.telegram_id),
                admin_ids={99001},
            )
            shop_less.bot_user_id = int(leftover.id)
            await session.flush()
            got = await resolve_bot_org_principal(
                session,
                db_user=leftover,
                is_reseller_bot=False,
                admin_ids=frozenset({99001}),
            )
            self.assertIsNone(got)

            result = await provision_level2_child(
                session,
                _l1_staff(a),
                Level2ProvisionRequest(
                    pg_username="shop_l2",
                    pg_password=_CHILD_PASSWORD,
                    pg_role_id=12,
                    idempotency_key="unify-shop-l2",
                ),
            )
            await session.flush()
            child = result.principal
            shop_user = await session.get(BotUser, int(child.bot_user_id))
            owner_bot = await resolve_bot_org_principal(
                session,
                db_user=shop_user,
                is_reseller_bot=False,
                admin_ids=frozenset({99001}),
            )
            self.assertIsNone(owner_bot)
            shop_bot = await resolve_bot_org_principal(
                session,
                db_user=shop_user,
                is_reseller_bot=True,
                reseller_profile_id=int(child.reseller_profile_id),
                reseller_owner_id=int(shop_user.id),
            )
            self.assertIsNotNone(shop_bot)
            self.assertEqual(int(shop_bot.id), int(child.id))

            shop = await session.get(ResellerProfile, int(child.reseller_profile_id))
            fake_child = ParentPg()
            with patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake_child),
            ), patch(
                "app.services.pasarguard.get_pg",
                return_value=self._platform,
            ) as spy_owner:
                await apply_reseller_panel_password(
                    session, shop, "NewPass12!@ab", sync_pg=True
                )
            self.assertTrue(fake_child.modify_admin.await_count)
            self.assertFalse(spy_owner.called)
            self.assertFalse(self._platform.modify_admin.await_count)

    def test_synthetic_telegram_id_is_not_authority(self) -> None:
        from app.services.platform_identity import (
            is_bot_platform_admin,
            is_synthetic_telegram_id,
        )
        from app.services.reseller_access import is_bot_admin_id, parse_telegram_ids

        self.assertTrue(is_synthetic_telegram_id(-99))
        self.assertTrue(is_synthetic_telegram_id(0))
        self.assertFalse(is_synthetic_telegram_id(1001))
        self.assertFalse(
            is_bot_platform_admin(
                type("U", (), {"role": "user", "telegram_id": -99})(),
                admin_ids={-99},
            )
        )
        self.assertNotIn(-99, parse_telegram_ids("-99,12"))
        profile = type("P", (), {"bot_admin_ids": "-99,12"})()
        self.assertFalse(is_bot_admin_id(profile, -99))
        self.assertTrue(is_bot_admin_id(profile, 12))


class RepresentativeUnificationSourceContracts(unittest.TestCase):
    def test_product_ui_is_resellers_not_principals_nav(self) -> None:
        from tests.sidebar_view import owner_sidebar, sidebar

        self.assertIn(("نمایندگان من", "/resellers"), sidebar("reseller", perms=("dashboard",), add_rep=True)["shop"])
        self.assertIn(("نمایندگان", "/resellers"), owner_sidebar()["shop"])
        self.assertNotIn("نمایندگان", [label for label, _ in sidebar("admin", owner=True)["shop"]])
        self.assertNotIn("/principals", [h for nav in owner_sidebar().values() for _, h in nav])
        pages = (ROOT / "app/api/principal_pages.py").read_text(encoding="utf-8")
        self.assertIn('dest = "/resellers"', pages)
        self.assertIn("assert_live_parent_for_child", pages)
        resellers = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("provision_level2_child", resellers)
        self.assertIn("/resellers/create-child", resellers)
        tpl = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertNotIn("Level 1", tpl)
        self.assertNotIn("Level 2", tpl)
        self.assertNotIn("Principal", tpl)
        self.assertNotIn('name="parent_id"', tpl)
        self.assertNotIn('name="depth"', tpl)


if __name__ == "__main__":
    unittest.main()
