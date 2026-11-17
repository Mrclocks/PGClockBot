"""Phase 4D — Bot PG node/host authorization on the Principal/Authz path."""

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
from app.services.bot_pg_object_pilot import (
    authorize_bot_pg_object_op,
    list_scoped_pg_objects,
    sanitize_pg_object_write_payload,
)
from app.services.bot_pg_user_pilot import authorize_bot_pg_user_op
from app.services.org_principals import (
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features
from app.services.platform_identity import is_explicit_owner_staff


def _obj(oid: int, owner: str | None, **extra) -> dict:
    row = {"id": oid, "name": f"obj_{oid}", **extra}
    if owner is not None:
        row["admin"] = {"username": owner}
    return row


VIEW_ONLY = {
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
        "reconnect": False,
        "stats": False,
    },
    "hosts": {"read": True, "create": False, "update": False, "delete": False},
    "users": {"read": True, "read_simple": True, "create": False, "update": False, "delete": False},
}
VIEW_UPDATE = {
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": True,
        "delete": False,
        "reconnect": False,
        "stats": False,
    },
    "hosts": {"read": True, "create": False, "update": True, "delete": False},
    "users": {"read": True, "read_simple": True, "create": False, "update": True, "delete": False},
}
FULL = {
    "nodes": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
        "reconnect": True,
        "stats": True,
    },
    "hosts": {"read": True, "create": True, "update": True, "delete": True},
    "users": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
        "reset_usage": True,
        "revoke_sub": True,
    },
}
NONE = {
    "nodes": {
        "read": False,
        "read_simple": False,
        "create": False,
        "update": False,
        "delete": False,
        "reconnect": False,
    },
    "hosts": {"read": False, "create": False, "update": False, "delete": False},
    "users": {"read": False, "read_simple": False, "create": False, "update": False, "delete": False},
}


def _role(permissions: dict, *, role_id: int = 10) -> dict:
    return {"id": role_id, "name": "ignored", "is_owner": False, "permissions": permissions}


def _fake_pg(
    *,
    nodes: dict[int, dict],
    hosts: dict[int, dict],
    listed_node_ids: set[int] | None = None,
    listed_host_ids: set[int] | None = None,
    users: dict[int, dict] | None = None,
):
    pg = AsyncMock()
    listed_n = listed_node_ids if listed_node_ids is not None else set(nodes)
    listed_h = listed_host_ids if listed_host_ids is not None else set(hosts)
    users = users or {}

    async def _get_node(nid):
        row = nodes.get(int(nid))
        if row is None:
            raise RuntimeError("pg node missing")
        return dict(row)

    async def _get_nodes():
        return [dict(nodes[i]) for i in listed_n if i in nodes]

    async def _get_host(hid):
        row = hosts.get(int(hid))
        if row is None:
            raise RuntimeError("pg host missing")
        return dict(row)

    async def _get_hosts():
        return [dict(hosts[i]) for i in listed_h if i in hosts]

    async def _get_user(uid):
        row = users.get(int(uid))
        if row is None:
            raise RuntimeError("pg user missing")
        return dict(row)

    pg.get_node = AsyncMock(side_effect=_get_node)
    pg.get_nodes = AsyncMock(side_effect=_get_nodes)
    pg.get_host = AsyncMock(side_effect=_get_host)
    pg.get_hosts = AsyncMock(side_effect=_get_hosts)
    pg.get_user_by_id = AsyncMock(side_effect=_get_user)
    pg.reconnect_node = AsyncMock(return_value=None)
    pg.reconnect_all_nodes = AsyncMock(return_value=None)
    pg.sync_node = AsyncMock(return_value=None)
    pg.reset_node = AsyncMock(return_value=None)
    pg.modify_node = AsyncMock(return_value=None)
    pg.delete_node = AsyncMock(return_value=None)
    pg.create_node = AsyncMock(return_value={"id": 501})
    pg.modify_host = AsyncMock(return_value=None)
    pg.delete_host = AsyncMock(return_value=None)
    pg.create_host = AsyncMock(return_value={"id": 601})
    return pg


class Phase4DBotPgObjectsTests(unittest.IsolatedAsyncioTestCase):
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
            telegram_id=77001, role=Role.USER.value, referral_code="own4d"
        )
        session.add(owner_user)
        ua, pa = await self._shop(session, tid=77010, code="a4d", uname="shopa")
        ub, pb = await self._shop(session, tid=77020, code="b4d", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        await create_principal(
            session, parent_id=int(pa_p.id), depth=2, pg_username="pg_a1"
        )
        stray = BotUser(
            telegram_id=77099, role=Role.ADMIN.value, referral_code="adm4d"
        )
        session.add(stray)
        await session.commit()
        nodes = {
            11: _obj(11, None, address="10.0.0.11"),  # L1 A, list-proven
            12: _obj(12, "pg_shopb", address="10.0.0.12"),  # sibling B
            13: _obj(13, "pg_shopa", address="10.0.0.13"),  # L1 A explicit owner
            19: _obj(19, None, address="10.0.0.19"),  # unknown, not in L1 list
            1: _obj(1, "env_owner", address="1.1.1.1"),
        }
        hosts = {
            21: _obj(21, None, remark="ha"),
            22: _obj(22, "pg_shopb", remark="hb"),
            23: _obj(23, "pg_shopa", remark="ha2"),
            29: _obj(29, None, remark="unknown"),
            2: _obj(2, "env_owner", remark="owner"),
        }
        users = {
            101: {
                "id": 101,
                "username": "vpn_101",
                "admin": {"username": "pg_shopa"},
            }
        }
        return SimpleNamespace(
            owner_p=owner_p,
            owner_user=owner_user,
            ua=ua,
            pa_p=pa_p,
            ub=ub,
            pb_p=pb_p,
            stray=stray,
            nodes=nodes,
            hosts=hosts,
            users=users,
        )

    def _owner_caps(self):
        return {
            "ok": True,
            "features": ["pg_nodes", "pg_hosts", "pg_users"],
            "pg_is_owner": True,
            "username": "env_owner",
            "role": {"is_owner": True},
        }

    def _patches(self, permissions: dict, fake_pg):
        role = _role(permissions)
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
                "app.services.bot_pg_object_authz.bot_pg_client_for_resolution",
                new=AsyncMock(return_value=(fake_pg, False)),
            ),
            patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={77001}),
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

    def _user_patches(self, permissions: dict, fake_pg):
        role = _role(permissions)
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
                "app.services.bot_pg_user_authz.bot_pg_client_for_resolution",
                new=AsyncMock(return_value=(fake_pg, False)),
            ),
            patch(
                "app.services.pg_quota.assert_can_mutate_owned_users",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={77001}),
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

    def _l1_pg(self, fx):
        return _fake_pg(
            nodes=fx.nodes,
            hosts=fx.hosts,
            listed_node_ids={11, 13},
            listed_host_ids={21, 23},
            users=fx.users,
        )

    def _owner_pg(self, fx):
        return _fake_pg(
            nodes=fx.nodes,
            hosts=fx.hosts,
            users=fx.users,
        )

    async def _auth(self, session, *, db_user, kind, action, permissions, fake_pg, **kwargs):
        patches = self._patches(permissions, fake_pg)
        with self._enter(patches):
            return await authorize_bot_pg_object_op(
                session,
                db_user=db_user,
                kind=kind,
                action=action,
                **kwargs,
            )

    def test_sanitize_strips_owner_fields(self) -> None:
        cleaned = sanitize_pg_object_write_payload(
            {
                "name": "n",
                "admin": "env_owner",
                "org_principal_id": 1,
                "web_owner": True,
                "pg_username": "x",
            }
        )
        self.assertEqual(cleaned.get("name"), "n")
        self.assertNotIn("admin", cleaned)
        self.assertNotIn("org_principal_id", cleaned)
        self.assertNotIn("web_owner", cleaned)
        self.assertNotIn("pg_username", cleaned)

    async def test_a_owner_node_list_read(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._owner_pg(fx)
            listed = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="nodes",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
            )
            self.assertTrue(listed.allowed, listed.reason)
            self.assertEqual(int(listed.resolution.principal.id), int(fx.owner_p.id))
            rows = await list_scoped_pg_objects(listed, kind="nodes")
            ids = {int(n["id"]) for n in rows}
            self.assertIn(11, ids)
            self.assertIn(12, ids)
            self.assertIn(19, ids)
            read = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="nodes",
                action="read",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:n:19",
            )
            self.assertTrue(read.allowed, read.reason)
            self.assertTrue(is_explicit_owner_staff(read.staff))

    async def test_b_owner_host_list_read(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._owner_pg(fx)
            listed = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="hosts",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
            )
            self.assertTrue(listed.allowed, listed.reason)
            rows = await list_scoped_pg_objects(listed, kind="hosts")
            ids = {int(h["id"]) for h in rows}
            self.assertIn(21, ids)
            self.assertIn(29, ids)
            read = await self._auth(
                session,
                db_user=fx.owner_user,
                kind="hosts",
                action="read",
                permissions=FULL,
                fake_pg=fake_pg,
                object_id=29,
            )
            self.assertTrue(read.allowed, read.reason)

    async def test_c_l1_own_node_host_access(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            nlist = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="list",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
            )
            self.assertTrue(nlist.allowed, nlist.reason)
            nids = {int(n["id"]) for n in await list_scoped_pg_objects(nlist, kind="nodes")}
            self.assertIn(11, nids)
            self.assertIn(13, nids)
            self.assertNotIn(12, nids)
            nread = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:n:11",
            )
            self.assertTrue(nread.allowed, nread.reason)
            hlist = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="list",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
            )
            self.assertTrue(hlist.allowed, hlist.reason)
            hids = {int(h["id"]) for h in await list_scoped_pg_objects(hlist, kind="hosts")}
            self.assertIn(21, hids)
            self.assertIn(23, hids)
            self.assertNotIn(22, hids)
            hread = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=21,
            )
            self.assertTrue(hread.allowed, hread.reason)
            self.assertFalse(is_explicit_owner_staff(nread.staff))

    async def test_d_l1_sibling_foreign_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            node_sib = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:n:12",
            )
            host_sib = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="update",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                object_id=22,
            )
            self.assertFalse(node_sib.allowed)
            self.assertEqual(node_sib.reason, "resource_out_of_scope")
            self.assertFalse(host_sib.allowed)
            self.assertEqual(host_sib.reason, "resource_out_of_scope")

    async def test_e_l1_mutation_with_permission_allow(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            upd = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="update",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:ntog:13",
            )
            recon = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="reconnect",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:recon:11",
            )
            host_upd = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="update",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                object_id=23,
            )
            self.assertTrue(upd.allowed, upd.reason)
            self.assertTrue(recon.allowed, recon.reason)
            self.assertTrue(host_upd.allowed, host_upd.reason)

    async def test_f_l1_mutation_without_permission_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            node_upd = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="update",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:ntog:13",
            )
            node_del = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="delete",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:ndel:13",
            )
            recon = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="reconnect",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:recon:11",
            )
            # PasarGuard has no hosts.delete: removing a host needs hosts.update.
            host_del = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="delete",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=23,
            )
            host_del_with_update = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="delete",
                permissions=VIEW_UPDATE,
                fake_pg=fake_pg,
                object_id=23,
            )
            self.assertFalse(node_upd.allowed)
            self.assertFalse(node_del.allowed)
            self.assertFalse(recon.allowed)
            self.assertFalse(host_del.allowed)
            self.assertTrue(host_del_with_update.allowed, host_del_with_update.reason)
            read = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:n:13",
            )
            self.assertTrue(read.allowed, read.reason)

    async def test_g_unknown_object_ownership_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            node = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:n:19",
            )
            host = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=29,
            )
            self.assertFalse(node.allowed)
            self.assertEqual(node.reason, "resource_out_of_scope")
            self.assertFalse(host.allowed)
            self.assertEqual(host.reason, "resource_out_of_scope")
            nlist = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="list",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
            )
            nids = {int(n["id"]) for n in await list_scoped_pg_objects(nlist, kind="nodes")}
            self.assertNotIn(19, nids)

    async def test_h_id_tampering_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            cases = [
                ("read", "adm:pg:n:12"),
                ("update", "adm:pg:ntog:19"),
                ("delete", "adm:pg:ndel:999"),
                ("reconnect", "adm:pg:recon:12"),
                ("read", "adm:pg:n:11:extra"),
            ]
            for action, data in cases:
                gate = await self._auth(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action=action,  # type: ignore[arg-type]
                    permissions=FULL,
                    fake_pg=fake_pg,
                    callback_data=data,
                )
                self.assertFalse(gate.allowed, data)

    async def test_i_identity_injection_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            cases = [
                ("list", "adm:pg:nodes:org_principal_id=1"),
                ("create", "adm:pg:ncreate:parent_id=1"),
                ("read", "adm:pg:n:11:org_depth=0"),
                ("delete", "adm:pg:ndel:11:web_owner=1"),
                ("update", "adm:pg:ntog:11:pg_username=env_owner"),
                ("reconnect", "adm:pg:recon:11:principal_id=1"),
                ("list", "adm:pg:nodes:depth=0"),
            ]
            for action, data in cases:
                gate = await self._auth(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action=action,  # type: ignore[arg-type]
                    permissions=FULL,
                    fake_pg=fake_pg,
                    callback_data=data,
                )
                self.assertFalse(gate.allowed, data)
                self.assertEqual(gate.reason, "identity_tamper", data)

    async def test_j_pg_outage_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_node = AsyncMock(side_effect=RuntimeError("down"))
            fake_pg.get_nodes = AsyncMock(side_effect=RuntimeError("down"))
            fake_pg.get_host = AsyncMock(side_effect=RuntimeError("down"))
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:n:11",
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "pg_outage")
            host = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="read",
                permissions=VIEW_ONLY,
                fake_pg=fake_pg,
                object_id=21,
            )
            self.assertFalse(host.allowed)
            self.assertEqual(host.reason, "pg_outage")

    async def test_k_disabled_principal_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.pa_p.status = "disabled"
            await session.commit()
            fake_pg = self._l1_pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
            )
            self.assertFalse(gate.allowed)

    async def test_l_l2_bot_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.stray,
                kind="nodes",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
            )
            self.assertFalse(gate.allowed)

    async def test_m_shop_bot_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
                is_reseller_bot=True,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "shop_bot_isolated")
            host = await self._auth(
                session,
                db_user=fx.ua,
                kind="hosts",
                action="list",
                permissions=FULL,
                fake_pg=fake_pg,
                is_reseller_bot=True,
            )
            self.assertFalse(host.allowed)
            self.assertEqual(host.reason, "shop_bot_isolated")

    async def test_n_l1_never_receives_owner_pg_client(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_shop_pg = self._l1_pg(fx)
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
                return_value=SimpleNamespace(admin_ids={77001}),
            ), patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(return_value=self._owner_caps()),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_shop_pg),
            ) as gr, patch(
                "app.services.pasarguard.get_pg",
                return_value=owner_pg,
            ) as gp:
                gate = await authorize_bot_pg_object_op(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action="list",
                    callback_data="adm:pg:nodes",
                )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_shop_pg)
            self.assertFalse(gate.as_owner_client)
            self.assertFalse(bool(gate.staff.get("pg_is_owner")))
            gp.assert_not_called()
            gr.assert_awaited()
            self.assertGreaterEqual(gr.await_count, 1)

    async def test_o_phase4b_4c_pg_user_unchanged(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            patches = self._user_patches(VIEW_ONLY, fake_pg)
            with self._enter(patches):
                read = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="read",
                    callback_data="adm:pg:u:101",
                )
            self.assertTrue(read.allowed, read.reason)
            self.assertEqual(int(read.pg_user["id"]), 101)

    async def test_missing_capability_denies_list(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                kind="nodes",
                action="list",
                permissions=NONE,
                fake_pg=fake_pg,
                callback_data="adm:pg:nodes",
            )
            self.assertFalse(gate.allowed)

    async def test_handler_list_and_delete_scoped(self) -> None:
        import app.bot.handlers.admin_pg_nodes as mod

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = self._l1_pg(fx)
            cb = SimpleNamespace(data="adm:pg:nodes", answer=AsyncMock(), message=MagicMock())
            patches = self._patches(FULL, fake_pg)
            with self._enter(patches), patch.object(
                mod, "_render_nodes_list", new=AsyncMock()
            ) as render:
                await mod.pg_nodes(cb, fx.ua, session=session)
            render.assert_awaited_once()

            cb_d = SimpleNamespace(data="adm:pg:ndel:13", answer=AsyncMock(), message=None)
            with self._enter(patches):
                await mod.pg_node_delete(cb_d, db_user=fx.ua, session=session)
            fake_pg.delete_node.assert_awaited_once_with(13)

            cb_f = SimpleNamespace(data="adm:pg:ndel:12", answer=AsyncMock(), message=None)
            fake_pg.delete_node.reset_mock()
            with self._enter(patches):
                await mod.pg_node_delete(cb_f, db_user=fx.ua, session=session)
            fake_pg.delete_node.assert_not_called()


if __name__ == "__main__":
    unittest.main()
