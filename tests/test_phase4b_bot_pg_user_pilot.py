"""Phase 4B — Bot PG-user card/disable authorization pilot."""

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
    callback_carries_identity_tamper,
    parse_pg_user_callback_id,
)
from app.services.org_principals import (
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features
from app.services.pg_user_scope import pg_user_in_staff_scope
from app.services.platform_identity import is_explicit_owner_staff


def _pg_user(uid: int, owner: str) -> dict:
    return {"id": uid, "username": f"vpn_{uid}", "admin": {"username": owner}}


USERS_VIEW_ONLY = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": False,
        "delete": False,
    }
}
USERS_VIEW_UPDATE = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": False,
        "update": True,
        "delete": False,
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


def _role(permissions: dict, *, role_id: int = 10, is_owner: bool = False) -> dict:
    return {
        "id": role_id,
        "name": "ignored",
        "is_owner": is_owner,
        "permissions": permissions,
    }


def _fake_pg(users: dict[int, dict]):
    pg = AsyncMock()

    async def _get(uid):
        row = users.get(int(uid))
        if row is None:
            raise RuntimeError("pg user missing")
        return dict(row)

    pg.get_user_by_id = AsyncMock(side_effect=_get)
    pg.set_disabled_by_id = AsyncMock(side_effect=lambda uid, _d: dict(users[int(uid)]))
    return pg


class Phase4BBotPgUserPilotTests(unittest.IsolatedAsyncioTestCase):
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
            telegram_id=tid,
            role=Role.RESELLER.value,
            referral_code=code,
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
            telegram_id=99001,
            role=Role.USER.value,
            referral_code="own4b",
        )
        session.add(owner_user)
        ua, pa = await self._shop(session, tid=99010, code="a4b", uname="shopa")
        ub, pb = await self._shop(session, tid=99020, code="b4b", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        a1 = await create_principal(
            session,
            parent_id=int(pa_p.id),
            depth=2,
            pg_username="pg_a1",
        )
        a2 = await create_principal(
            session,
            parent_id=int(pa_p.id),
            depth=2,
            pg_username="pg_a2",
        )
        b1 = await create_principal(
            session,
            parent_id=int(pb_p.id),
            depth=2,
            pg_username="pg_b1",
        )
        stray = BotUser(
            telegram_id=99099,
            role=Role.ADMIN.value,
            referral_code="adm4b",
        )
        session.add(stray)
        await session.commit()
        users = {
            101: _pg_user(101, "pg_shopa"),
            102: _pg_user(102, "pg_shopb"),
            201: _pg_user(201, "pg_a1"),
            202: _pg_user(202, "pg_a2"),
            301: _pg_user(301, "pg_b1"),
            1: _pg_user(1, "env_owner"),
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
            b1=b1,
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

    def _l1_patches(self, permissions: dict, fake_pg):
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

    async def _auth(
        self,
        session,
        *,
        db_user,
        action: str,
        uid: int,
        permissions: dict,
        fake_pg,
        is_reseller_bot: bool = False,
        callback_data: str | None = None,
    ):
        kind = "read" if action == "read" else "disable"
        data = callback_data or (
            f"adm:pg:u:{uid}" if kind == "read" else f"adm:pg:dis:{uid}"
        )
        patches = self._l1_patches(permissions, fake_pg)
        with self._enter(patches):
            return await authorize_bot_pg_user_op(
                session,
                db_user=db_user,
                action=action,  # type: ignore[arg-type]
                callback_data=data,
                is_reseller_bot=is_reseller_bot,
            )

    def test_callback_parsers_reject_extra_fields(self) -> None:
        self.assertEqual(parse_pg_user_callback_id("adm:pg:u:9", kind="read"), 9)
        self.assertIsNone(
            parse_pg_user_callback_id("adm:pg:u:9:org_principal_id=1", kind="read")
        )
        self.assertEqual(parse_pg_user_callback_id("adm:pg:dis:7", kind="disable"), 7)
        self.assertIsNone(
            parse_pg_user_callback_id("adm:pg:dis:7:depth=0", kind="disable")
        )
        self.assertTrue(callback_carries_identity_tamper("adm:pg:dis:7:pg_username=x"))
        self.assertTrue(callback_carries_identity_tamper("adm:pg:u:1:org_depth=0"))
        self.assertFalse(callback_carries_identity_tamper("adm:pg:u:1"))

    async def test_owner_read_own_via_explicit_principal(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.owner_user,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertEqual(int(gate.resolution.principal.id), int(fx.owner_p.id))
            self.assertTrue(is_explicit_owner_staff(gate.staff))

    async def test_a_own_resource_allow(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
            )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertEqual(int(gate.resolution.principal.id), int(fx.pa_p.id))

    async def test_a_to_b_resource_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=102,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")
            fake_pg.set_disabled_by_id.assert_not_called()

    async def test_l2_a1_cannot_resolve_parent_or_sibling(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            # No Bot↔L2 mapping in this phase — unmapped actor DENY.
            gate_parent = await self._auth(
                session,
                db_user=fx.stray,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate_parent.allowed)

            staff_a1 = {
                "pg_admin_username": "pg_a1",
                "pg_is_owner": False,
                "org_depth": 2,
                "web_owner": False,
            }
            self.assertFalse(pg_user_in_staff_scope(fx.users[101], staff_a1))
            self.assertFalse(pg_user_in_staff_scope(fx.users[202], staff_a1))
            self.assertFalse(pg_user_in_staff_scope(fx.users[102], staff_a1))
            self.assertTrue(pg_user_in_staff_scope(fx.users[201], staff_a1))

    async def test_view_only_read_allow_mutation_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            read = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
            )
            mut = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                uid=101,
                permissions=USERS_VIEW_ONLY,
                fake_pg=fake_pg,
            )
            self.assertTrue(read.allowed, read.reason)
            self.assertFalse(mut.allowed)
            self.assertEqual(mut.reason, "pg_permission_denied")

    async def test_view_update_allows_read_and_disable(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            read = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            mut = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertTrue(read.allowed, read.reason)
            self.assertTrue(mut.allowed, mut.reason)

    async def test_missing_capability_denies_both(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            read = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_NONE,
                fake_pg=fake_pg,
            )
            mut = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                uid=101,
                permissions=USERS_NONE,
                fake_pg=fake_pg,
            )
            self.assertFalse(read.allowed)
            self.assertFalse(mut.allowed)

    async def test_role_admin_without_owner_mapping_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.stray,
                action="read",
                uid=1,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate.allowed)
            self.assertIn(gate.reason, {"missing_principal", "unauthenticated"})

    async def test_shop_bot_isolated(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                is_reseller_bot=True,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "shop_bot_isolated")

    async def test_callback_resource_id_tamper_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                uid=102,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")

    async def test_callback_principal_injection_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data=f"adm:pg:u:101:org_principal_id={fx.owner_p.id}",
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "identity_tamper")

    async def test_callback_depth_and_pg_username_injection_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            depth = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:dis:101:org_depth=0",
            )
            uname = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
                callback_data="adm:pg:u:101:pg_username=env_owner",
            )
            self.assertFalse(depth.allowed)
            self.assertEqual(depth.reason, "identity_tamper")
            self.assertFalse(uname.allowed)
            self.assertEqual(uname.reason, "identity_tamper")

    async def test_disabled_principal_denies(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.pa_p.status = "disabled"
            await session.commit()
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate.allowed)

    async def test_missing_principal_denies(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            ghost = BotUser(
                telegram_id=99111,
                role=Role.RESELLER.value,
                referral_code="ghost4b",
            )
            session.add(ghost)
            await session.commit()
            fake_pg = _fake_pg({101: _pg_user(101, "pg_x")})
            gate = await self._auth(
                session,
                db_user=ghost,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate.allowed)

    async def test_pg_outage_fail_closed(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_by_id = AsyncMock(side_effect=RuntimeError("down"))
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="read",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "pg_outage")

    async def test_l1_does_not_use_owner_pg_client(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            gate = await self._auth(
                session,
                db_user=fx.ua,
                action="disable",
                uid=101,
                permissions=USERS_VIEW_UPDATE,
                fake_pg=fake_pg,
            )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)
            self.assertFalse(is_explicit_owner_staff(gate.staff))
            self.assertFalse(bool(gate.staff.get("pg_is_owner")))

    async def test_handler_disable_allow_and_deny(self) -> None:
        import app.bot.handlers.admin_pg_users as mod

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            cb = SimpleNamespace(
                data="adm:pg:dis:101",
                answer=AsyncMock(),
                message=None,
            )
            patches = self._l1_patches(USERS_VIEW_UPDATE, fake_pg)
            with self._enter(patches):
                await mod.pg_dis(cb, db_user=fx.ua, session=session, state=ReasonedState(cb.data))
            fake_pg.set_disabled_by_id.assert_awaited_once_with(101, True)

            cb_b = SimpleNamespace(
                data="adm:pg:dis:102",
                answer=AsyncMock(),
                message=None,
            )
            fake_pg.set_disabled_by_id.reset_mock()
            with self._enter(patches):
                await mod.pg_dis(cb_b, db_user=fx.ua, session=session, state=ReasonedState(cb_b.data))
            fake_pg.set_disabled_by_id.assert_not_called()

    async def test_handler_detail_read(self) -> None:
        import app.bot.handlers.admin_pg_users as mod

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = _fake_pg(fx.users)
            cb = SimpleNamespace(
                data="adm:pg:u:101",
                answer=AsyncMock(),
                message=MagicMock(),
            )
            patches = self._l1_patches(USERS_VIEW_ONLY, fake_pg)
            with self._enter(patches), patch.object(
                mod, "_show_user_card", new=AsyncMock()
            ) as show:
                await mod.pg_user_detail(cb, db_user=fx.ua, session=session)
            show.assert_awaited_once()
            fake_pg.set_disabled_by_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
