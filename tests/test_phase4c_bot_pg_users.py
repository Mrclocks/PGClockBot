"""Phase 4C — remaining Bot PG-user operations on the Principal/Authz path."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.reason_support import ReasonedState
from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, ResellerProfile, Role
from app.services.bot_pg_user_pilot import (
    authorize_bot_pg_user_op,
    list_scoped_pg_users,
    lookup_scoped_pg_user_by_username,
    sanitize_pg_user_write_payload,
)
from app.services.org_principals import (
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features
from app.services.platform_identity import is_explicit_owner_staff


def _pg_user(uid: int, owner: str | None) -> dict:
    if owner is None:
        return {"id": uid, "username": f"vpn_{uid}"}
    return {"id": uid, "username": f"vpn_{uid}", "admin": {"username": owner}}


USERS_VIEW_ONLY = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
        "reset_usage": False,
        "revoke_sub": False,
    }
}
USERS_VIEW_UPDATE = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": True,
        "delete": False,
        "reset_usage": False,
        "revoke_sub": False,
    }
}
USERS_CREATE = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": False,
        "delete": False,
    }
}
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
USERS_NONE = {
    "users": {
        "read": False,
        "read_simple": False,
        "create": False,
        "update": False,
        "delete": False,
    }
}


def _role(permissions: dict, *, role_id: int = 10) -> dict:
    return {"id": role_id, "name": "ignored", "is_owner": False, "permissions": permissions}


def _fake_pg(users: dict[int, dict]):
    pg = AsyncMock()

    async def _get(uid):
        row = users.get(int(uid))
        if row is None:
            raise RuntimeError("pg user missing")
        return dict(row)

    async def _list(**_kwargs):
        return {"users": [dict(u) for u in users.values()], "total": len(users)}

    async def _by_name(name):
        for u in users.values():
            if u.get("username") == name:
                return dict(u)
        raise RuntimeError("missing")

    pg.get_user_by_id = AsyncMock(side_effect=_get)
    pg.get_users = AsyncMock(side_effect=_list)
    pg.get_user_by_username = AsyncMock(side_effect=_by_name)
    pg.set_disabled_by_id = AsyncMock(side_effect=lambda uid, _d: dict(users[int(uid)]))
    pg.reset_user_by_id = AsyncMock(side_effect=lambda uid: dict(users[int(uid)]))
    pg.revoke_sub_by_id = AsyncMock(side_effect=lambda uid: dict(users[int(uid)]))
    pg.delete_user_by_id = AsyncMock(return_value=None)
    pg.create_user = AsyncMock(return_value={"id": 501, "admin": {"username": "pg_shopa"}})
    pg.create_user_from_template = AsyncMock(
        return_value={"id": 502, "admin": {"username": "pg_shopa"}}
    )
    pg.modify_user_by_id = AsyncMock(side_effect=lambda uid, _p: dict(users[int(uid)]))
    return pg


class Phase4CBotPgUsersTests(unittest.IsolatedAsyncioTestCase):
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
            telegram_id=99001, role=Role.USER.value, referral_code="own4c"
        )
        session.add(owner_user)
        ua, pa = await self._shop(session, tid=99010, code="a4c", uname="shopa")
        ub, pb = await self._shop(session, tid=99020, code="b4c", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        await create_principal(
            session, parent_id=int(pa_p.id), depth=2, pg_username="pg_a1"
        )
        stray = BotUser(
            telegram_id=99099, role=Role.ADMIN.value, referral_code="adm4c"
        )
        session.add(stray)
        await session.commit()
        users = {
            101: _pg_user(101, "pg_shopa"),
            102: _pg_user(102, "pg_shopb"),
            109: _pg_user(109, None),
            1: _pg_user(1, "env_owner"),
        }
        return SimpleNamespace(
            owner_p=owner_p,
            owner_user=owner_user,
            ua=ua,
            pa_p=pa_p,
            ub=ub,
            pb_p=pb_p,
            stray=stray,
            users=users,
        )

    def _owner_caps(self):
        return {
            "ok": True,
            "features": ["pg_users"],
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
                "app.services.bot_pg_user_authz.bot_pg_client_for_resolution",
                new=AsyncMock(return_value=(fake_pg, False)),
            ),
            patch(
                "app.services.pg_quota.assert_can_mutate_owned_users",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.pg_quota.assert_can_create_user",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={99001}),
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

    async def _auth(self, session, *, db_user, action, permissions, fake_pg, **kwargs):
        patches = self._patches(permissions, fake_pg)
        with self._enter(patches):
            return await authorize_bot_pg_user_op(
                session,
                db_user=db_user,
                action=action,
                **kwargs,
            )

    def test_sanitize_strips_owner_fields(self) -> None:
        cleaned = sanitize_pg_user_write_payload(
            {
                "username": "n",
                "admin": "env_owner",
                "org_principal_id": 1,
                "web_owner": True,
            }
        )
        self.assertEqual(cleaned.get("username"), "n")
        self.assertNotIn("admin", cleaned)
        self.assertNotIn("org_principal_id", cleaned)
        self.assertNotIn("web_owner", cleaned)

    async def test_a_owner_list_search_mutate(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            listed = await self._auth(
                session,
                db_user=fx.owner_user,
                action="list",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertTrue(listed.allowed, listed.reason)
            self.assertEqual(int(listed.resolution.principal.id), int(fx.owner_p.id))
            rows, _ = await list_scoped_pg_users(listed)
            self.assertGreaterEqual(len(rows), 2)
            reset = await self._auth(
                session,
                db_user=fx.owner_user,
                action="reset",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:reset:101",
            )
            self.assertTrue(reset.allowed, reset.reason)
            self.assertTrue(is_explicit_owner_staff(reset.staff))

    async def test_b_l1_own_list_search(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            listed = await self._auth(
                session,
                db_user=fx.ua,
                action="list",
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertTrue(listed.allowed, listed.reason)
            rows, _ = await list_scoped_pg_users(listed)
            ids = {int(u["id"]) for u in rows}
            self.assertIn(101, ids)
            self.assertNotIn(102, ids)
            hit = await lookup_scoped_pg_user_by_username(listed, "vpn_101")
            self.assertIsNotNone(hit)
            miss = await lookup_scoped_pg_user_by_username(listed, "vpn_102")
            self.assertIsNone(miss)

    async def test_c_l1_list_does_not_leak_b(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            listed = await self._auth(
                session,
                db_user=fx.ua,
                action="list",
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            rows, _ = await list_scoped_pg_users(listed)
            self.assertFalse(any(int(u["id"]) == 102 for u in rows))

    async def test_d_l1_create_requires_capability(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            denied = await self._auth(
                session,
                db_user=fx.ua,
                action="create",
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:create",
            )
            allowed = await self._auth(
                session,
                db_user=fx.ua,
                action="create",
                permissions=USERS_CREATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:create",
            )
            self.assertFalse(denied.allowed)
            self.assertTrue(allowed.allowed, allowed.reason)
            self.assertFalse(is_explicit_owner_staff(allowed.staff))

    async def test_e_l1_create_not_owner_staff(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="create",
                permissions=USERS_CREATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:create",
            )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)
            self.assertFalse(bool(gate.staff.get("pg_is_owner")))

    async def test_f_g_l1_edit_own_vs_foreign(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            own = await self._auth(
                session,
                db_user=fx.ua,
                action="update",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:101:edit",
            )
            foreign = await self._auth(
                session,
                db_user=fx.ua,
                action="update",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:102:edit",
            )
            self.assertTrue(own.allowed, own.reason)
            self.assertFalse(foreign.allowed)
            self.assertEqual(foreign.reason, "resource_out_of_scope")

    async def test_h_reset_capability_allow_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            deny = await self._auth(
                session,
                db_user=fx.ua,
                action="reset",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:reset:101",
            )
            allow = await self._auth(
                session,
                db_user=fx.ua,
                action="reset",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:reset:101",
            )
            self.assertFalse(deny.allowed)
            self.assertTrue(allow.allowed, allow.reason)

    async def test_i_enable_revoke_delete(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            en = await self._auth(
                session,
                db_user=fx.ua,
                action="enable",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:en:101",
            )
            rev_deny = await self._auth(
                session,
                db_user=fx.ua,
                action="revoke",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:rev:101",
            )
            rev_ok = await self._auth(
                session,
                db_user=fx.ua,
                action="revoke",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:rev:101",
            )
            foreign_del = await self._auth(
                session,
                db_user=fx.ua,
                action="delete",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:102:del",
            )
            own_del = await self._auth(
                session,
                db_user=fx.ua,
                action="delete",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:101:del",
            )
            self.assertTrue(en.allowed, en.reason)
            self.assertFalse(rev_deny.allowed)
            self.assertTrue(rev_ok.allowed, rev_ok.reason)
            self.assertFalse(foreign_del.allowed)
            self.assertTrue(own_del.allowed, own_del.reason)

    async def test_j_missing_capability_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            listed = await self._auth(
                session,
                db_user=fx.ua,
                action="list",
                permissions=USERS_NONE,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertFalse(listed.allowed)

    async def test_k_pg_outage_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_by_id = AsyncMock(side_effect=RuntimeError("down"))
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="enable",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:en:101",
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "pg_outage")

    async def test_l_unknown_ownership_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="update",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:109:edit",
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")
            listed = await self._auth(
                session,
                db_user=fx.ua,
                action="list",
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            rows, _ = await list_scoped_pg_users(listed)
            self.assertFalse(any(int(u["id"]) == 109 for u in rows))

    async def test_m_callback_tamper_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            cases = [
                ("list", "adm:pg:users:org_principal_id=1"),
                ("create", "adm:pg:create:parent_id=1"),
                ("reset", "adm:pg:reset:101:org_depth=0"),
                ("delete", "adm:pg:u:101:del:web_owner=1"),
                ("update", "adm:pg:u:101:edit:pg_username=env_owner"),
                ("enable", "adm:pg:en:101:org_scope=global"),
            ]
            for action, data in cases:
                gate = await self._auth(
                    session,
                    db_user=fx.ua,
                    action=action,  # type: ignore[arg-type]
                    permissions=USERS_FULL,
                    fake_pg=fake_pg,
                    callback_data=data,
                )
                self.assertFalse(gate.allowed, data)
                self.assertEqual(gate.reason, "identity_tamper", data)

    async def test_n_l2_unmapped_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.stray,
                action="list",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertFalse(gate.allowed)

    async def test_o_shop_bot_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="list",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
                is_reseller_bot=True,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "shop_bot_isolated")

    async def test_p_role_admin_alone_not_owner(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.stray,
                action="list",
                permissions=USERS_FULL,
                fake_pg=fake_pg,
                callback_data="adm:pg:users",
            )
            self.assertFalse(gate.allowed)

    async def test_q_detail_disable_still_work(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            read = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:101",
            )
            dis = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:dis:101",
            )
            self.assertTrue(read.allowed, read.reason)
            self.assertTrue(dis.allowed, dis.reason)

    async def test_handler_list_and_reset(self) -> None:
        import app.bot.handlers.admin_pg_users as mod

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            cb = SimpleNamespace(data="adm:pg:users", answer=AsyncMock(), message=MagicMock())
            state = AsyncMock()
            state.update_data = AsyncMock()
            patches = self._patches(USERS_FULL, fake_pg)
            with self._enter(patches), patch.object(
                mod, "_render_users_list", new=AsyncMock()
            ) as render:
                await mod.pg_users_list(cb, state, fx.ua, session=session)
            render.assert_awaited_once()

            cb_r = SimpleNamespace(data="adm:pg:reset:101", answer=AsyncMock(), message=None)
            with self._enter(patches):
                await mod.pg_reset(cb_r, db_user=fx.ua, session=session, state=ReasonedState(cb_r.data))
            fake_pg.reset_user_by_id.assert_awaited_once_with(101)

            cb_f = SimpleNamespace(data="adm:pg:reset:102", answer=AsyncMock(), message=None)
            fake_pg.reset_user_by_id.reset_mock()
            with self._enter(patches):
                await mod.pg_reset(cb_f, db_user=fx.ua, session=session, state=ReasonedState(cb_f.data))
            fake_pg.reset_user_by_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
