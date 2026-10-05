"""Phase 4E — Bot PG template/group authorization on the Principal/Authz path."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, ResellerProfile, Role
from app.services.bot_pg_catalog_pilot import (
    authorize_bot_pg_catalog_op,
    list_scoped_pg_catalog,
    sanitize_pg_catalog_write_payload,
)
from app.services.bot_pg_object_pilot import authorize_bot_pg_object_op
from app.services.bot_pg_user_pilot import authorize_bot_pg_user_op
from app.services.org_principals import (
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features
from app.services.platform_identity import is_explicit_owner_staff


def _item(oid: int, name: str) -> dict:
    return {"id": oid, "name": name}


VIEW_ONLY = {
    "templates": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
    },
    "groups": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
    },
    "users": {"read": True, "read_simple": True, "create": False, "update": False, "delete": False},
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
        "reconnect": False,
    },
}
VIEW_UPDATE = {
    "templates": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": True,
        "delete": False,
    },
    "groups": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": True,
        "delete": False,
    },
    "users": {"read": True, "read_simple": True, "create": False, "update": True, "delete": False},
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": True,
        "delete": False,
        "reconnect": False,
    },
}
FULL = {
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
    "users": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
        "reset_usage": True,
        "revoke_sub": True,
    },
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
        "reconnect": True,
    },
}

OWN_ACCESS = {"allowed_template_ids": [10], "allowed_group_ids": [1]}


def _role(permissions: dict, *, access: dict | None = OWN_ACCESS) -> dict:
    out = {"id": 10, "name": "ignored", "is_owner": False, "permissions": permissions}
    if access is not None:
        out["access"] = access
    return out


def _fake_pg(*, templates: dict[int, dict], groups: dict[int, dict], users: dict[int, dict] | None = None, nodes: dict[int, dict] | None = None):
    pg = AsyncMock()
    users = users or {}
    nodes = nodes or {}

    async def _get_tpl(tid):
        row = templates.get(int(tid))
        if row is None:
            raise RuntimeError("pg template missing")
        return dict(row)

    async def _list_tpl():
        return [dict(v) for v in templates.values()]

    async def _get_grp(gid):
        row = groups.get(int(gid))
        if row is None:
            raise RuntimeError("pg group missing")
        return dict(row)

    async def _list_grp():
        return [dict(v) for v in groups.values()]

    async def _get_user(uid):
        row = users.get(int(uid))
        if row is None:
            raise RuntimeError("pg user missing")
        return dict(row)

    async def _get_node(nid):
        row = nodes.get(int(nid))
        if row is None:
            raise RuntimeError("pg node missing")
        return dict(row)

    async def _list_nodes():
        return [dict(v) for v in nodes.values()]

    pg.get_user_template = AsyncMock(side_effect=_get_tpl)
    pg.get_user_templates = AsyncMock(side_effect=_list_tpl)
    pg.get_user_templates_simple = AsyncMock(side_effect=_list_tpl)
    pg.create_user_template = AsyncMock(return_value={"id": 501})
    pg.modify_user_template = AsyncMock(side_effect=lambda tid, _p: dict(templates[int(tid)]))
    pg.delete_user_template = AsyncMock(return_value=None)
    pg.get_group = AsyncMock(side_effect=_get_grp)
    pg.get_groups = AsyncMock(side_effect=_list_grp)
    pg.get_groups_simple = AsyncMock(side_effect=_list_grp)
    pg.create_group = AsyncMock(return_value={"id": 601})
    pg.modify_group = AsyncMock(side_effect=lambda gid, _p: dict(groups[int(gid)]))
    pg.delete_group = AsyncMock(return_value=None)
    pg.get_user_by_id = AsyncMock(side_effect=_get_user)
    pg.get_node = AsyncMock(side_effect=_get_node)
    pg.get_nodes = AsyncMock(side_effect=_list_nodes)
    return pg


class Phase4EBotPgCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _shop(self, session, *, tid: int, code: str, uname: str):
        user = BotUser(
            telegram_id=tid, role=Role.RESELLER.value, referral_code=code
        )
        session.add(user)
        await session.flush()
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
        owner_user = BotUser(
            telegram_id=66001, role=Role.USER.value, referral_code="own4e"
        )
        session.add(owner_user)
        ua, pa = await self._shop(session, tid=66010, code="a4e", uname="shopa")
        ub, pb = await self._shop(session, tid=66020, code="b4e", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        await create_principal(
            session, parent_id=int(pa_p.id), depth=2, pg_username="pg_a1"
        )
        stray = BotUser(
            telegram_id=66099, role=Role.ADMIN.value, referral_code="adm4e"
        )
        session.add(stray)
        await session.commit()
        templates = {
            10: _item(10, "tpl_a"),
            20: _item(20, "tpl_b"),
            99: _item(99, "tpl_unknown"),
        }
        groups = {
            1: _item(1, "grp_a"),
            2: _item(2, "grp_b"),
            9: _item(9, "grp_unknown"),
        }
        users = {
            101: {"id": 101, "username": "vpn_101", "admin": {"username": "pg_shopa"}}
        }
        nodes = {11: {"id": 11, "name": "n11", "admin": {"username": "pg_shopa"}}}
        return SimpleNamespace(
            owner_p=owner_p,
            owner_user=owner_user,
            ua=ua,
            pa_p=pa_p,
            ub=ub,
            pb_p=pb_p,
            stray=stray,
            templates=templates,
            groups=groups,
            users=users,
            nodes=nodes,
        )

    def _owner_caps(self):
        return {
            "ok": True,
            "features": ["pg_templates", "pg_groups", "pg_users", "pg_nodes"],
            "pg_is_owner": True,
            "username": "env_owner",
            "role": {"is_owner": True},
        }

    def _patches(self, permissions: dict, fake_pg, *, access: dict | None = OWN_ACCESS):
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
                "app.services.bot_pg_catalog_pilot.bot_pg_client_for_resolution",
                new=AsyncMock(return_value=(fake_pg, False)),
            ),
            patch(
                "app.services.pg_quota.assert_can_mutate_owned_users",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={66001}),
            ),
            patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(return_value=self._owner_caps()),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_pg),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake_pg),
            ),
        )

    def _enter(self, patches):
        stack = ExitStack()
        for p in patches:
            stack.enter_context(p)
        return stack

    def _pg(self, fx):
        return _fake_pg(
            templates=fx.templates,
            groups=fx.groups,
            users=fx.users,
            nodes=fx.nodes,
        )

    async def _auth(
        self,
        session,
        *,
        db_user,
        kind,
        action,
        permissions,
        fake_pg,
        access: dict | None = OWN_ACCESS,
        **kwargs,
    ):
        patches = self._patches(permissions, fake_pg, access=access)
        with self._enter(patches):
            return await authorize_bot_pg_catalog_op(
                session,
                db_user=db_user,
                kind=kind,
                action=action,
                **kwargs,
            )

    def test_sanitize_strips_owner_fields(self) -> None:
        cleaned = sanitize_pg_catalog_write_payload(
            {"name": "t", "admin": "env_owner", "org_principal_id": 1, "web_owner": True}
        )
        self.assertEqual(cleaned.get("name"), "t")
        self.assertNotIn("admin", cleaned)
        self.assertNotIn("org_principal_id", cleaned)
        self.assertNotIn("web_owner", cleaned)

    async def test_a_owner_template_list_read(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            listed = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="templates",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:template",
            )
            self.assertTrue(listed.allowed, listed.reason)
            self.assertEqual(int(listed.resolution.principal.id), int(fx.owner_p.id))
            rows = await list_scoped_pg_catalog(listed, kind="templates")
            ids = {int(t["id"]) for t in rows}
            self.assertIn(10, ids)
            self.assertIn(20, ids)
            self.assertIn(99, ids)
            read = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="templates",
                action="read",
                permissions=FULL,
                fake_pg=fake_pg,
                object_id=99,
            )
            self.assertTrue(read.allowed, read.reason)
            self.assertTrue(is_explicit_owner_staff(read.staff))

    async def test_b_owner_template_mutation(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            upd = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="templates",
                action="update",
                permissions=FULL,
                fake_pg=fake_pg,
                object_id=10,
                group_ids=[1],
            )
            created = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="templates",
                action="create",
                permissions=FULL,
                fake_pg=fake_pg,
                group_ids=[1, 2],
            )
            deleted = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="templates",
                action="delete",
                permissions=FULL,
                fake_pg=fake_pg,
                object_id=20,
            )
            self.assertTrue(upd.allowed, upd.reason)
            self.assertTrue(created.allowed, created.reason)
            self.assertTrue(deleted.allowed, deleted.reason)

    async def test_c_l1_own_allowed_template(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            listed = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="list",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:template",
            )
            self.assertTrue(listed.allowed, listed.reason)
            ids = {int(t["id"]) for t in await list_scoped_pg_catalog(listed, kind="templates")}
            self.assertEqual(ids, {10})
            read = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=10,
            )
            self.assertTrue(read.allowed, read.reason)
            self.assertFalse(is_explicit_owner_staff(read.staff))

    async def test_d_l1_foreign_template_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=20,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")

    async def test_e_missing_invalid_template_allow_list_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            # Open allow-list (None) on credentialed L1 → own-client IDs OK (web parity).
            missing = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=10,
                access={},
            )
            invalid = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=10,
                access={"allowed_template_ids": "not-a-list", "allowed_group_ids": [1]},
            )
            empty = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="list",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:template",
                access={"allowed_template_ids": [], "allowed_group_ids": [1]},
            )
            self.assertTrue(missing.allowed, missing.reason)
            self.assertFalse(invalid.allowed)
            self.assertTrue(empty.allowed, empty.reason)
            self.assertEqual(await list_scoped_pg_catalog(empty, kind="templates"), [])

    async def test_f_l1_group_list_read_when_allowed(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            listed = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="list",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:group",
            )
            self.assertTrue(listed.allowed, listed.reason)
            ids = {int(g["id"]) for g in await list_scoped_pg_catalog(listed, kind="groups")}
            self.assertEqual(ids, {1})
            read = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=1,
            )
            self.assertTrue(read.allowed, read.reason)

    async def test_g_l1_foreign_group_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=2,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")

    async def test_h_missing_invalid_group_allow_list_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            # Open allow-list on credentialed L1 → selecting listed groups is allowed.
            missing = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=1,
                access={},
            )
            invalid = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=1,
                access={"allowed_group_ids": "nope", "allowed_template_ids": [10]},
            )
            self.assertTrue(missing.allowed, missing.reason)
            self.assertFalse(invalid.allowed)

    async def test_i_view_only_mutation_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            tpl = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="update",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=10,
            )
            grp = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="delete",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=1,
            )
            self.assertFalse(tpl.allowed)
            self.assertFalse(grp.allowed)
            read = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=10,
            )
            self.assertTrue(read.allowed, read.reason)

    async def test_j_mutation_capability_allow(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            upd = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="update",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                object_id=10,
                group_ids=[1],
            )
            grp_upd = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="update",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                object_id=1,
            )
            created = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="create",
                permissions=FULL,
                fake_pg=fake_pg,
                group_ids=[1],
            )
            self.assertTrue(upd.allowed, upd.reason)
            self.assertTrue(grp_upd.allowed, grp_upd.reason)
            self.assertTrue(created.allowed, created.reason)

    async def test_k_id_tampering_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            cases = [
                ("read", "adm:pg:settpl:20"),
                ("read", "adm:pg:settpl:999"),
                ("list", "adm:pg:template:10"),
                ("update", "adm:pg:toggrp:2"),
            ]
            for action, data in cases:
                kind = "groups" if "grp" in data else "templates"
                gate = await self._auth(
                    session,
                    db_user=fx.ua,
                    kind=kind,  # type: ignore[arg-type]
                    action=action,  # type: ignore[arg-type]
                    permissions=FULL,
                    fake_pg=fake_pg,
                    callback_data=data,
                )
                self.assertFalse(gate.allowed, data)

    async def test_l_identity_injection_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            cases = [
                ("list", "adm:pg:template:org_principal_id=1"),
                ("list", "adm:pg:group:parent_id=1"),
                ("read", "adm:pg:settpl:10:org_depth=0"),
                ("update", "adm:pg:toggrp:1:web_owner=1"),
                ("create", "adm:pg:template:pg_username=env_owner"),
                ("list", "adm:pg:template:principal_id=1"),
                ("list", "adm:pg:group:depth=0"),
            ]
            for action, data in cases:
                kind = "groups" if "group" in data or "grp" in data else "templates"
                gate = await self._auth(
                    session,
                    db_user=fx.ua,
                    kind=kind,  # type: ignore[arg-type]
                    action=action,  # type: ignore[arg-type]
                    permissions=FULL,
                    fake_pg=fake_pg,
                    callback_data=data,
                )
                self.assertFalse(gate.allowed, data)
                self.assertEqual(gate.reason, "identity_tamper", data)

    async def test_m_pg_outage_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_template = AsyncMock(side_effect=RuntimeError("down"))
            fake_pg.get_group = AsyncMock(side_effect=RuntimeError("down"))
            tpl = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=10,
            )
            grp = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=1,
            )
            self.assertFalse(tpl.allowed)
            self.assertEqual(tpl.reason, "pg_outage")
            self.assertFalse(grp.allowed)
            self.assertEqual(grp.reason, "pg_outage")

    async def test_n_disabled_principal_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.pa_p.status = "disabled"
            await session.commit()
            fake_pg = self._pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:template",
            )
            self.assertFalse(gate.allowed)

    async def test_o_l2_bot_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.stray,
                kind="templates",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:template",
            )
            self.assertFalse(gate.allowed)

    async def test_p_shop_bot_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="templates",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:template",
                is_reseller_bot=True,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "shop_bot_isolated")
            grp = await self._auth(
                session,
                db_user=fx.ua,
                kind="groups",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:group",
                is_reseller_bot=True,
            )
            self.assertFalse(grp.allowed)
            self.assertEqual(grp.reason, "shop_bot_isolated")

    async def test_q_l1_never_receives_owner_pg_client(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_shop_pg = self._pg(fx)
            owner_pg = MagicMock()
            role = _role(FULL)
            features = map_pg_role_to_features(role)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=10),
            ), patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=(features, role)),
            ), patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={66001}),
            ), patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(return_value=self._owner_caps()),
            ), patch(
                "app.services.pg_quota.assert_can_mutate_owned_users",
                new=AsyncMock(return_value=None),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_shop_pg),
            ) as gr, patch(
                "app.services.pasarguard.get_pg",
                return_value=owner_pg,
            ) as gp:
                gate = await authorize_bot_pg_catalog_op(
                    session,
                    db_user=fx.ua,
                    kind="templates",
                    action="list",
                    callback_data="adm:pg:template",
                )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_shop_pg)
            self.assertFalse(gate.as_owner_client)
            self.assertFalse(bool(gate.staff.get("pg_is_owner")))
            gp.assert_not_called()
            gr.assert_awaited()
            self.assertGreaterEqual(gr.await_count, 1)

    async def test_r_phase4b_4c_4d_unchanged(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            role = _role(FULL)
            features = map_pg_role_to_features(role)
            common = (
                patch(
                    "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                    new=AsyncMock(return_value=10),
                ),
                patch(
                    "app.services.pg_access.resolve_reseller_pg_features",
                    new=AsyncMock(return_value=(features, role)),
                ),
                patch(
                    "app.config.get_settings",
                    return_value=SimpleNamespace(admin_ids={66001}),
                ),
                patch(
                    "app.services.pg_access.resolve_platform_pg_capabilities",
                    new=AsyncMock(return_value=self._owner_caps()),
                ),
                patch(
                    "app.services.pg_quota.assert_can_mutate_owned_users",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "app.services.pasarguard.get_pg_for_reseller",
                    new=AsyncMock(return_value=fake_pg),
                ),
                patch(
                    "app.services.pasarguard.get_pg_for_principal",
                    new=AsyncMock(return_value=fake_pg),
                ),
            )
            with common[0], common[1], common[2], common[3], common[4], common[5], common[6], patch(
                "app.services.bot_pg_user_pilot.bot_pg_client_for_resolution",
                new=AsyncMock(return_value=(fake_pg, False)),
            ):
                user = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="read",
                    callback_data="adm:pg:u:101",
                )
            self.assertTrue(user.allowed, user.reason)
            with common[0], common[1], common[2], common[3], common[4], common[5], common[6], patch(
                "app.services.bot_pg_object_pilot.bot_pg_client_for_resolution",
                new=AsyncMock(return_value=(fake_pg, False)),
            ):
                node = await authorize_bot_pg_object_op(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action="read",
                    callback_data="adm:pg:n:11",
                )
            self.assertTrue(node.allowed, node.reason)

    async def test_handler_template_group_hints(self) -> None:
        import app.bot.handlers.admin as mod

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._pg(fx)
            patches = self._patches(FULL, fake_pg)
            cb = SimpleNamespace(
                data="adm:pg:template",
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock()),
            )
            with self._enter(patches):
                await mod.adm_pg_template_hint(cb, fx.ua, session=session)
            cb.message.edit_text.assert_awaited()
            text = cb.message.edit_text.await_args.args[0]
            self.assertIn("tpl_a", text)
            self.assertNotIn("tpl_b", text)

            cb_g = SimpleNamespace(
                data="adm:pg:group",
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock()),
            )
            with self._enter(patches):
                await mod.adm_pg_group_hint(cb_g, fx.ua, session=session)
            shown = cb_g.message.edit_text.await_args.args[0]
            self.assertIn("grp_a", shown)
            self.assertNotIn("grp_b", shown)


if __name__ == "__main__":
    unittest.main()
