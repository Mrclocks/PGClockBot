"""Phase 5C — L2 Bot PG authorization parity with existing 4B–4E gates."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, ResellerProfile, Role
from app.bot.auth import (
    OWNER_REQUIRED_MESSAGE,
    is_migrated_pg_soft_callback,
)
from app.services.bot_l2_bind import bind_l2_bot_telegram
from app.services.bot_pg_catalog_pilot import authorize_bot_pg_catalog_op
from app.services.bot_pg_object_pilot import authorize_bot_pg_object_op
from app.services.bot_pg_user_pilot import (
    authorize_bot_pg_user_op,
    list_scoped_pg_users,
)
from app.services.bot_principal_identity import bot_pg_client_for_resolution
from app.services.org_principals import (
    attach_org_principal_fields,
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features


USERS_FULL = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
        "reset_usage": True,
        "revoke_sub": True,
    }
}
USERS_VIEW = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
    }
}
NODES_FULL = {
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
        "reconnect": True,
    }
}
NODES_VIEW = {
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
        "reconnect": False,
    }
}
HOSTS_FULL = {
    "hosts": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
    }
}
CATALOG_FULL = {
    "templates": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
    },
    "groups": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
    },
}
OWN_ACCESS = {"allowed_template_ids": [10], "allowed_group_ids": [1]}


def _role(permissions: dict, *, access: dict | None = OWN_ACCESS) -> dict:
    out = {"id": 10, "name": "ignored", "is_owner": False, "permissions": permissions}
    if access is not None:
        out["access"] = access
    return out


def _pg_user(uid: int, admin: str | None) -> dict:
    row: dict = {"id": uid, "username": f"vpn_{uid}"}
    if admin:
        row["admin"] = {"username": admin}
    return row


def _obj(oid: int, admin: str | None, **extra) -> dict:
    row: dict = {"id": oid, **extra}
    if admin:
        row["admin"] = {"username": admin}
    return row


class Phase5CBotL2PgParityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _user(self, session, *, tid: int, code: str, role: str = Role.USER.value):
        user = BotUser(telegram_id=tid, role=role, referral_code=code)
        session.add(user)
        await session.flush()
        return user

    async def _shop(self, session, *, tid: int, code: str, uname: str):
        user = await self._user(session, tid=tid, code=code, role=Role.RESELLER.value)
        profile = ResellerProfile(
            user_id=user.id,
            is_active=True,
            web_username=uname,
            web_password_hash="x" * 24,
            setup_completed_at=datetime.now(timezone.utc),
            pg_admin_username=f"pg_{uname}",
            pg_admin_password_enc="enc",
            pg_role_id=10,
        )
        session.add(profile)
        await session.flush()
        return user, profile

    async def _fixtures(self, session):
        owner_p = await ensure_owner_principal(session)
        owner_user = await self._user(
            session, tid=66001, code="own5c", role=Role.ADMIN.value
        )
        ua, pa = await self._shop(session, tid=66010, code="a5c", uname="shopa")
        ub, pb = await self._shop(session, tid=66020, code="b5c", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        a1 = await create_principal(
            session,
            parent_id=int(pa_p.id),
            depth=2,
            pg_username="pg_a1",
            pg_password_enc="enc",
        )
        a2 = await create_principal(
            session,
            parent_id=int(pa_p.id),
            depth=2,
            pg_username="pg_a2",
            pg_password_enc="enc",
        )
        l2_user = await self._user(session, tid=66101, code="l2a1")
        sib_user = await self._user(session, tid=66102, code="l2a2")
        await bind_l2_bot_telegram(
            session,
            staff=attach_org_principal_fields(
                {
                    "role": "reseller",
                    "bot_user_id": int(ua.id),
                    "reseller_profile_id": int(pa.id),
                },
                pa_p,
            ),
            target_principal_id=int(a1.id),
            telegram_id=int(l2_user.telegram_id),
            admin_ids={66001},
        )
        await bind_l2_bot_telegram(
            session,
            staff=attach_org_principal_fields(
                {
                    "role": "reseller",
                    "bot_user_id": int(ua.id),
                    "reseller_profile_id": int(pa.id),
                },
                pa_p,
            ),
            target_principal_id=int(a2.id),
            telegram_id=int(sib_user.telegram_id),
            admin_ids={66001},
        )
        l2_shop = ResellerProfile(
            user_id=int(l2_user.id),
            is_active=True,
            web_username="l2a1shop",
            web_password_hash="x" * 24,
            setup_completed_at=datetime.now(timezone.utc),
            pg_admin_username="pg_a1",
            pg_role_id=10,
        )
        sib_shop = ResellerProfile(
            user_id=int(sib_user.id),
            is_active=True,
            web_username="l2a2shop",
            web_password_hash="x" * 24,
            setup_completed_at=datetime.now(timezone.utc),
            pg_admin_username="pg_a2",
            pg_role_id=10,
        )
        session.add_all([l2_shop, sib_shop])
        await session.flush()
        a1.reseller_profile_id = int(l2_shop.id)
        a1.bot_user_id = int(l2_user.id)
        a2.reseller_profile_id = int(sib_shop.id)
        a2.bot_user_id = int(sib_user.id)
        await session.commit()
        users = {
            201: _pg_user(201, "pg_a1"),
            202: _pg_user(202, "pg_shopa"),
            203: _pg_user(203, "pg_a2"),
            1: _pg_user(1, "env_owner"),
            209: _pg_user(209, None),
        }
        nodes = {
            31: _obj(31, "pg_a1", name="n-l2"),
            32: _obj(32, "pg_shopa", name="n-l1"),
            33: _obj(33, "pg_a2", name="n-sib"),
            39: _obj(39, None, name="n-unk"),
        }
        hosts = {
            41: _obj(41, "pg_a1", remark="h-l2"),
            42: _obj(42, "pg_shopa", remark="h-l1"),
        }
        templates = {
            10: {"id": 10, "name": "tpl_own"},
            20: {"id": 20, "name": "tpl_other"},
        }
        groups = {
            1: {"id": 1, "name": "grp_own"},
            2: {"id": 2, "name": "grp_other"},
        }
        return SimpleNamespace(
            owner_p=owner_p,
            owner_user=owner_user,
            ua=ua,
            pa=pa,
            pa_p=pa_p,
            ub=ub,
            pb=pb,
            pb_p=pb_p,
            a1=a1,
            a2=a2,
            l2_user=l2_user,
            sib_user=sib_user,
            l2_shop=l2_shop,
            users=users,
            nodes=nodes,
            hosts=hosts,
            templates=templates,
            groups=groups,
        )

    def _admin_ids(self, *ids: int):
        return patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(admin_ids=set(ids)),
        )

    def _l2_patches(self, permissions: dict, fake_pg, *, access: dict | None = OWN_ACCESS):
        role = _role(permissions, access=access)
        features = map_pg_role_to_features(role)
        return (
            patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=10),
            ),
            patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=(features, role)),
            ),
            patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("L2 must not use Owner get_pg()"),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_pg),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake_pg),
            ),
            patch(
                "app.services.pg_quota.assert_can_mutate_owned_users",
                new=AsyncMock(return_value=None),
            ),
            self._admin_ids(66001),
        )

    def _fake_pg(self, fx):
        pg = AsyncMock()

        async def _get_user(uid):
            row = fx.users.get(int(uid))
            if row is None:
                raise RuntimeError("missing user")
            return dict(row)

        async def _list_users(**_kwargs):
            return {"users": [dict(u) for u in fx.users.values()]}

        async def _get_node(nid):
            row = fx.nodes.get(int(nid))
            if row is None:
                raise RuntimeError("missing node")
            return dict(row)

        async def _list_nodes():
            return [dict(v) for v in fx.nodes.values() if v.get("admin", {}).get("username") == "pg_a1"]

        async def _get_host(hid):
            row = fx.hosts.get(int(hid))
            if row is None:
                raise RuntimeError("missing host")
            return dict(row)

        async def _get_tpl(tid):
            row = fx.templates.get(int(tid))
            if row is None:
                raise RuntimeError("missing tpl")
            return dict(row)

        async def _get_grp(gid):
            row = fx.groups.get(int(gid))
            if row is None:
                raise RuntimeError("missing grp")
            return dict(row)

        pg.get_user_by_id = AsyncMock(side_effect=_get_user)
        pg.get_users = AsyncMock(side_effect=_list_users)
        pg.get_node = AsyncMock(side_effect=_get_node)
        pg.get_nodes = AsyncMock(side_effect=_list_nodes)
        pg.get_host = AsyncMock(side_effect=_get_host)
        pg.get_hosts = AsyncMock(return_value=[dict(fx.hosts[41])])
        pg.get_user_template = AsyncMock(side_effect=_get_tpl)
        pg.get_user_templates = AsyncMock(
            return_value=[dict(v) for v in fx.templates.values()]
        )
        pg.get_group = AsyncMock(side_effect=_get_grp)
        pg.get_groups = AsyncMock(return_value=[dict(v) for v in fx.groups.values()])
        return pg

    def _l2_kw(self, fx) -> dict:
        return {
            "is_reseller_bot": True,
            "reseller_profile_id": int(fx.l2_shop.id),
            "reseller_owner_id": int(fx.l2_user.id),
        }

    async def _auth_user(self, session, fx, *, action, perms, fake_pg, **kwargs):
        patches = self._l2_patches(perms, fake_pg)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            return await authorize_bot_pg_user_op(
                session, db_user=fx.l2_user, action=action, **self._l2_kw(fx), **kwargs
            )

    async def test_1_2_l2_lists_own_users_not_parent(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            gate = await self._auth_user(
                session, fx, action="list", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)
            rows, _ = await list_scoped_pg_users(gate)
            ids = {int(u["id"]) for u in rows}
            self.assertIn(201, ids)
            self.assertNotIn(202, ids)
            self.assertNotIn(203, ids)
            self.assertNotIn(1, ids)

    async def test_3_l2_cannot_see_sibling_users(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            gate = await self._auth_user(
                session, fx, action="read", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:u:203",
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")

    async def test_4_l2_cannot_mutate_foreign_user(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            parent = await self._auth_user(
                session, fx, action="delete", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:u:202:del",
            )
            owner = await self._auth_user(
                session, fx, action="delete", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:u:1:del",
            )
            unknown = await self._auth_user(
                session, fx, action="update", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:u:209:edit",
            )
            self.assertFalse(parent.allowed)
            self.assertFalse(owner.allowed)
            self.assertFalse(unknown.allowed)

    async def test_5_6_own_mutate_requires_capability(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            ok = await self._auth_user(
                session, fx, action="delete", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:u:201:del",
            )
            view = await self._auth_user(
                session, fx, action="delete", perms=USERS_VIEW, fake_pg=fake_pg,
                callback_data="adm:pg:u:201:del",
            )
            read = await self._auth_user(
                session, fx, action="read", perms=USERS_VIEW, fake_pg=fake_pg,
                callback_data="adm:pg:u:201",
            )
            self.assertTrue(ok.allowed, ok.reason)
            self.assertFalse(view.allowed)
            self.assertEqual(view.reason, "pg_permission_denied")
            self.assertTrue(read.allowed, read.reason)

    async def test_7_8_9_nodes_and_hosts(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            patches = self._l2_patches({**NODES_FULL, **HOSTS_FULL}, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                own = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="nodes", action="read",
                    callback_data="adm:pg:n:31",
                )
                parent_n = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="nodes", action="read",
                    callback_data="adm:pg:n:32",
                )
                sib_n = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="nodes", action="read",
                    callback_data="adm:pg:n:33",
                )
                host_own = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="hosts", action="read",
                    object_id=41,
                )
                host_foreign = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="hosts", action="read",
                    object_id=42,
                )
            self.assertTrue(own.allowed, own.reason)
            self.assertFalse(parent_n.allowed)
            self.assertFalse(sib_n.allowed)
            self.assertTrue(host_own.allowed, host_own.reason)
            self.assertFalse(host_foreign.allowed)

            view_p = self._l2_patches(NODES_VIEW, fake_pg)
            with view_p[0], view_p[1], view_p[2], view_p[3], view_p[4], view_p[5], view_p[6]:
                mut = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="nodes", action="delete",
                    callback_data="adm:pg:ndel:31",
                )
                rd = await authorize_bot_pg_object_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="nodes", action="read",
                    callback_data="adm:pg:n:31",
                )
            self.assertFalse(mut.allowed)
            self.assertTrue(rd.allowed, rd.reason)

    async def test_10_11_templates_groups_allow_list(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            patches = self._l2_patches(CATALOG_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                own_t = await authorize_bot_pg_catalog_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="templates", action="read",
                    callback_data="adm:pg:settpl:10",
                )
                other_t = await authorize_bot_pg_catalog_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="templates", action="read",
                    callback_data="adm:pg:settpl:20",
                )
                own_g = await authorize_bot_pg_catalog_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="groups", action="update",
                    callback_data="adm:pg:toggrp:1",
                )
                other_g = await authorize_bot_pg_catalog_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="groups", action="update",
                    callback_data="adm:pg:toggrp:2",
                )
            self.assertTrue(own_t.allowed, own_t.reason)
            self.assertFalse(other_t.allowed)
            self.assertTrue(own_g.allowed, own_g.reason)
            self.assertFalse(other_g.allowed)

            empty = self._l2_patches(CATALOG_FULL, fake_pg, access={})
            with empty[0], empty[1], empty[2], empty[3], empty[4], empty[5], empty[6]:
                missing = await authorize_bot_pg_catalog_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="templates", action="read",
                    callback_data="adm:pg:settpl:10",
                )
            # Open allow-list (None) on credentialed L2 → own-client IDs OK (web parity / #502).
            self.assertTrue(missing.allowed, missing.reason)

            closed = self._l2_patches(
                CATALOG_FULL,
                fake_pg,
                access={"allowed_template_ids": [], "allowed_group_ids": []},
            )
            with closed[0], closed[1], closed[2], closed[3], closed[4], closed[5], closed[6]:
                empty_list = await authorize_bot_pg_catalog_op(
                    session, db_user=fx.l2_user, **self._l2_kw(fx), kind="templates", action="read",
                    callback_data="adm:pg:settpl:10",
                )
            self.assertFalse(empty_list.allowed)

    async def test_12_13_l2_client_is_own_principal_only(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = object()
            patches = self._l2_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                from app.services.bot_principal_identity import resolve_bot_principal_bridge

                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
                assert bridge is not None
                client, as_owner = await bot_pg_client_for_resolution(session, bridge)
                gate = await authorize_bot_pg_user_op(
                    session, db_user=fx.l2_user, action="list",
                    callback_data="adm:pg:users",
                    **self._l2_kw(fx),
                )
            self.assertIs(client, fake_pg)
            self.assertFalse(as_owner)
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)
            self.assertEqual(int(bridge.principal.id), int(fx.a1.id))
            self.assertEqual(bridge.staff.get("pg_admin_username"), "pg_a1")
            self.assertEqual(int(bridge.staff.get("reseller_profile_id") or 0), int(fx.l2_shop.id))

    async def test_14_pg_outage_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            patches = self._l2_patches(USERS_FULL, AsyncMock())
            with patches[0], patches[1], patches[2], patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(side_effect=RuntimeError("pg down")),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(side_effect=RuntimeError("pg down")),
            ), patches[5], patches[6]:
                gate = await authorize_bot_pg_user_op(
                    session, db_user=fx.l2_user, action="list",
                    callback_data="adm:pg:users",
                    **self._l2_kw(fx),
                )
            self.assertFalse(gate.allowed)
            self.assertIn(gate.reason, {"pg_outage", "pg_capabilities_unavailable"})

    async def test_15_disabled_l2_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.a1.status = "disabled"
            await session.commit()
            fake_pg = self._fake_pg(fx)
            gate = await self._auth_user(
                session, fx, action="list", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertFalse(gate.allowed)

    async def test_16_disabled_l1_parent_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.pa_p.status = "disabled"
            await session.commit()
            fake_pg = self._fake_pg(fx)
            gate = await self._auth_user(
                session, fx, action="list", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertFalse(gate.allowed)

    async def test_17_shop_bot_l2_telegram_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            patches = self._l2_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    action="list",
                    callback_data="adm:pg:users",
                    is_reseller_bot=True,
                    reseller_profile_id=int(fx.pa.id),
                    reseller_owner_id=int(fx.ua.id),
                )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "shop_bot_isolated")

    async def test_18_callback_spoof_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            gate = await self._auth_user(
                session, fx, action="read", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:u:201:org_principal_id:1:org_depth:0:pg_username:env_owner",
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "identity_tamper")

    async def test_19_l2_cannot_access_owner_surfaces(self) -> None:
        from app.bot.handlers.admin import pg_stats
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.reply_nav import (
            open_admin_backup_hub,
            open_admin_broadcast_hub,
            open_admin_plans_hub,
            open_admin_settings_hub,
        )

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._fake_pg(fx)
            patches = self._l2_patches(USERS_FULL, fake_pg)
            cb = SimpleNamespace(data="adm:backup:create", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                await backup_create(cb, fx.l2_user, session=session)
                await pg_stats(
                    SimpleNamespace(data="adm:pg:stats", answer=AsyncMock(), message=None),
                    fx.l2_user,
                    session=session,
                )
            self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])
            msg = SimpleNamespace(
                answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
                from_user=SimpleNamespace(id=1),
                bot=SimpleNamespace(),
                text="x",
            )
            with patches[6]:
                await open_admin_backup_hub(msg, session, fx.l2_user, None, is_reseller_bot=False)
                await open_admin_settings_hub(msg, session, fx.l2_user, None, is_reseller_bot=False)
                await open_admin_broadcast_hub(msg, session, fx.l2_user, None, is_reseller_bot=False)
                await open_admin_plans_hub(msg, session, fx.l2_user, SimpleNamespace(), is_reseller_bot=False)
            self.assertIn("مالک", msg.answer.await_args.args[0])
        self.assertFalse(is_migrated_pg_soft_callback("adm:backup"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:pg:stats"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:pg:admins"))

    async def test_20_l1_behavior_unchanged(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_users = AsyncMock(
                return_value={"users": [_pg_user(202, "pg_shopa")]}
            )
            role = _role(USERS_FULL)
            features = map_pg_role_to_features(role)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=10),
            ), patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=(features, role)),
            ), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("L1 must not use Owner get_pg()"),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_pg),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                side_effect=AssertionError("L1 Bot still uses get_pg_for_reseller"),
            ), self._admin_ids(66001):
                gate = await authorize_bot_pg_user_op(
                    session, db_user=fx.ua, action="list",
                    callback_data="adm:pg:users",
                )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)

    async def test_21_owner_behavior_unchanged(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_users = AsyncMock(return_value={"users": []})
            with self._admin_ids(66001), patch(
                "app.services.pasarguard.get_pg",
                return_value=fake_pg,
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                side_effect=AssertionError("Owner must not use reseller client"),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                side_effect=AssertionError("Owner must not use principal client"),
            ), patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "features": ["pg_users"],
                        "pg_is_owner": True,
                        "username": "env_owner",
                        "role": {"is_owner": True},
                    }
                ),
            ):
                gate = await authorize_bot_pg_user_op(
                    session, db_user=fx.owner_user, action="list",
                    callback_data="adm:pg:users",
                )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)

    async def test_22_missing_l2_credentials_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.a1.pg_password_enc = None
            await session.commit()
            fake_pg = self._fake_pg(fx)
            gate = await self._auth_user(
                session, fx, action="list", perms=USERS_FULL, fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertFalse(gate.allowed)


if __name__ == "__main__":
    unittest.main()
