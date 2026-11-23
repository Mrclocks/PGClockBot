"""Phase 4G — unify Bot Owner authorization and enable L1 PG family parity."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
from app.bot.auth import (
    OWNER_REQUIRED_MESSAGE,
    is_bot_owner_principal,
    is_migrated_pg_soft_callback,
    require_bot_owner,
)
from app.services.bot_pg_catalog_authz import authorize_bot_pg_catalog_op
from app.services.bot_pg_object_authz import authorize_bot_pg_object_op
from app.services.bot_pg_user_authz import authorize_bot_pg_user_op
from app.services.bot_principal_identity import bot_pg_client_for_resolution
from app.services.org_principals import (
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features
from tests.postgres_support import scoped_async_engine


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


def _role(permissions: dict, *, access: dict | None = OWN_ACCESS, is_owner: bool = False) -> dict:
    out = {"id": 10, "name": "ignored", "is_owner": is_owner, "permissions": permissions}
    if access is not None:
        out["access"] = access
    return out


def _fake_msg():
    bubble = SimpleNamespace(
        edit_text=AsyncMock(),
        from_user=SimpleNamespace(id=1),
        bot=SimpleNamespace(),
    )
    msg = SimpleNamespace(
        answer=AsyncMock(return_value=bubble),
        from_user=SimpleNamespace(id=1),
        bot=SimpleNamespace(),
        text="x",
        edit_text=AsyncMock(),
    )
    return msg, bubble


class Phase4GBotOwnerL1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
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
            telegram_id=77001,
            role=Role.ADMIN.value,
            referral_code="own4g",
        )
        session.add(owner_user)
        sticky = BotUser(
            telegram_id=77002,
            role=Role.ADMIN.value,
            referral_code="stk4g",
        )
        session.add(sticky)
        ua, pa = await self._shop(session, tid=77010, code="a4g", uname="shopa")
        ub, pb = await self._shop(session, tid=77020, code="b4g", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        l2 = await create_principal(
            session, parent_id=int(pa_p.id), depth=2, pg_username="pg_a1"
        )
        await session.commit()
        return SimpleNamespace(
            owner_p=owner_p,
            owner_user=owner_user,
            sticky=sticky,
            ua=ua,
            pa=pa,
            pa_p=pa_p,
            ub=ub,
            pb=pb,
            pb_p=pb_p,
            l2=l2,
        )

    def _admin_ids(self, *ids: int):
        return patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(admin_ids=set(ids)),
        )

    def _l1_patches(self, permissions: dict, fake_pg, *, access: dict | None = OWN_ACCESS):
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
                side_effect=AssertionError("L1 must not use Owner get_pg()"),
            ),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_pg),
            ),
            self._admin_ids(77001),
        )

    # ----- Part A -----

    async def test_1_role_admin_without_owner_principal_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self._admin_ids():
                self.assertFalse(
                    await is_bot_owner_principal(session, fx.sticky, is_reseller_bot=False)
                )
                self.assertFalse(
                    await require_bot_owner(
                        session, fx.sticky, is_reseller_bot=False, notify=False
                    )
                )

    async def test_2_role_admin_fake_web_owner_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            cb = SimpleNamespace(
                data="adm:backup:create:web_owner:1:org_principal_id:1",
                answer=AsyncMock(),
            )
            with self._admin_ids():
                ok = await require_bot_owner(
                    session, fx.sticky, is_reseller_bot=False, callback=cb
                )
            self.assertFalse(ok)
            cb.answer.assert_awaited()
            self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])

    async def test_3_admin_ids_active_owner_allow(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self._admin_ids(77001):
                self.assertTrue(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )

    async def test_4_admin_ids_missing_owner_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self._admin_ids(77001), patch(
                "app.services.bot_principal_identity.ensure_owner_principal",
                new=AsyncMock(side_effect=RuntimeError("missing owner")),
            ):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )

    async def test_5_inactive_owner_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            owner_id = int(fx.owner_p.id)
            user_id = int(fx.owner_user.id)
            session.add(OrgPrincipal(parent_id=None, depth=0, status="disabled"))
            with self.assertRaises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with self.Session() as session:
            owner_p = await session.get(OrgPrincipal, owner_id)
            owner_user = await session.get(BotUser, user_id)
            self.assertIsNotNone(owner_p)
            self.assertIsNotNone(owner_user)
            owner_p.status = "disabled"
            await session.commit()
            await session.refresh(owner_user)
            with self._admin_ids(77001):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, owner_user, is_reseller_bot=False
                    )
                )

        async with self.Session() as session:
            owner_p = await session.get(OrgPrincipal, owner_id)
            owner_user = await session.get(BotUser, user_id)
            self.assertIsNotNone(owner_p)
            self.assertIsNotNone(owner_user)
            owner_p.status = "disabled"
            await session.commit()
            await session.refresh(owner_user)
            with self._admin_ids(77001):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, owner_user, is_reseller_bot=False
                    )
                )

    async def _reply_owner_only(self, opener, fx, session) -> None:

        msg, _ = _fake_msg()
        with self._admin_ids(), patch(
            "app.services.backup.list_backups",
            side_effect=AssertionError("backup list must not run"),
        ):
            await opener(
                msg, session, fx.sticky, None, is_reseller_bot=False
            )
        self.assertTrue(msg.answer.await_count)
        text = msg.answer.await_args.args[0]
        self.assertIn("مالک", text)

    async def test_6_reply_nav_backup_owner_only(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_backup_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._reply_owner_only(open_admin_backup_hub, fx, session)

    async def test_7_reply_nav_settings_owner_only(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_settings_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_settings_hub(
                    msg, session, fx.sticky, None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_8_reply_nav_broadcast_owner_only(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_broadcast_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_broadcast_hub(
                    msg, session, fx.sticky, None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_9_reply_nav_platform_plans_owner_only(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_plans_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_plans_hub(
                    msg, session, fx.sticky, MagicMock(), is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_10_reply_nav_reseller_provisioning_owner_only(self) -> None:
        from app.bot.handlers.reply_nav import _soft_admin

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, bubble = _fake_msg()
            with self._admin_ids(), patch(
                "app.bot.handlers.admin.adm_resellers_add",
                new=AsyncMock(side_effect=AssertionError("must not provision")),
            ):
                await _soft_admin(
                    msg,
                    session,
                    fx.sticky,
                    "adm:resellers:add",
                    None,
                    is_reseller_bot=False,
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])
            self.assertEqual(bubble.edit_text.await_count, 0)

    async def test_11_reply_nav_pg_overview_owner_only(self) -> None:
        from app.bot.handlers.reply_nav import _soft_admin

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids(), patch(
                "app.bot.handlers.admin.pg_stats",
                new=AsyncMock(side_effect=AssertionError("overview is owner-only")),
            ):
                await _soft_admin(
                    msg,
                    session,
                    fx.sticky,
                    "adm:pg:stats",
                    None,
                    is_reseller_bot=False,
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_12_shop_bot_owner_ops_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self._admin_ids(77001):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=True
                    )
                )
            from app.bot.handlers.reply_nav import open_admin_backup_hub

            msg, _ = _fake_msg()
            await open_admin_backup_hub(
                msg, session, fx.owner_user, None, is_reseller_bot=True
            )
            self.assertIn("ربات اصلی", msg.answer.await_args.args[0])

    async def test_13_callback_tampering_deny(self) -> None:
        from app.services.bot_principal_identity import callback_carries_principal_tamper

        self.assertTrue(
            callback_carries_principal_tamper("adm:pg:u:1:web_owner:1")
        )
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="read",
                    callback_data="adm:pg:u:101:org_principal_id:1",
                    pg_user_id=101,
                )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "identity_tamper")

    def test_soft_callback_classifier(self) -> None:
        self.assertTrue(is_migrated_pg_soft_callback("adm:pg:users"))
        self.assertTrue(is_migrated_pg_soft_callback("adm:pg:nodes"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:backup"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:pg:stats"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:settings"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:plans"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:resellers:add"))

    # ----- Part B -----

    async def test_14_l1_pg_users_live_with_permission(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_users = AsyncMock(
                return_value={
                    "users": [
                        {"id": 101, "username": "vpn_101", "admin": {"username": "pg_shopa"}}
                    ]
                }
            )
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_user_op(
                    session, db_user=fx.ua, action="list", callback_data="adm:pg:users"
                )
            self.assertTrue(gate.allowed, gate.reason)
            self.assertIs(gate.pg_client, fake_pg)

            from app.bot.handlers.admin_pg_users import pg_users_list

            cb = SimpleNamespace(
                data="adm:pg:users",
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            )
            state = SimpleNamespace(update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await pg_users_list(
                    cb, state, fx.ua, session=session, is_reseller_bot=False
                )
            cb.answer.assert_awaited()

    async def test_15_l1_pg_users_foreign_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_by_id = AsyncMock(
                return_value={
                    "id": 202,
                    "username": "vpn_b",
                    "admin": {"username": "pg_shopb"},
                }
            )
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="read",
                    callback_data="adm:pg:u:202",
                    pg_user_id=202,
                )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")

    async def test_16_l1_nodes_live_with_permission(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_nodes = AsyncMock(
                return_value=[{"id": 11, "name": "n11", "admin": {"username": "pg_shopa"}}]
            )
            patches = self._l1_patches(NODES_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_object_op(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action="list",
                    callback_data="adm:pg:nodes",
                )
            self.assertTrue(gate.allowed, gate.reason)

    async def test_17_l1_foreign_node_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_node = AsyncMock(
                return_value={"id": 22, "name": "n22", "admin": {"username": "pg_shopb"}}
            )
            patches = self._l1_patches(NODES_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_object_op(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action="read",
                    callback_data="adm:pg:n:22",
                    object_id=22,
                )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "resource_out_of_scope")

    async def test_18_l1_templates_groups_allow_list(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_templates_simple = AsyncMock(
                return_value=[{"id": 10, "name": "tpl_a"}, {"id": 20, "name": "tpl_b"}]
            )
            fake_pg.get_groups_simple = AsyncMock(
                return_value=[{"id": 1, "name": "grp_a"}, {"id": 2, "name": "grp_b"}]
            )
            perms = {**CATALOG_FULL, **USERS_VIEW}
            patches = self._l1_patches(perms, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                tgate = await authorize_bot_pg_catalog_op(
                    session,
                    db_user=fx.ua,
                    kind="templates",
                    action="list",
                    callback_data="adm:pg:template",
                )
                ggate = await authorize_bot_pg_catalog_op(
                    session,
                    db_user=fx.ua,
                    kind="groups",
                    action="list",
                    callback_data="adm:pg:group",
                )
            self.assertTrue(tgate.allowed, tgate.reason)
            self.assertTrue(ggate.allowed, ggate.reason)

    async def test_19_l1_unresolved_allow_list_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches(CATALOG_FULL, fake_pg, access={})
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_catalog_op(
                    session,
                    db_user=fx.ua,
                    kind="templates",
                    action="read",
                    callback_data="adm:pg:settpl:10",
                    object_id=10,
                )
            self.assertFalse(gate.allowed)

    async def test_20_l1_mutation_without_capability_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_by_id = AsyncMock(
                return_value={
                    "id": 101,
                    "username": "vpn_101",
                    "admin": {"username": "pg_shopa"},
                }
            )
            patches = self._l1_patches(USERS_VIEW, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="delete",
                    callback_data="adm:pg:u:101:del",
                    pg_user_id=101,
                )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "pg_permission_denied")

    async def test_21_l1_never_receives_owner_pg_client(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = object()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                from app.services.bot_principal_identity import resolve_bot_principal_bridge

                resolution = await resolve_bot_principal_bridge(
                    session, db_user=fx.ua, is_reseller_bot=False
                )
                self.assertIsNotNone(resolution)
                client, as_owner = await bot_pg_client_for_resolution(session, resolution)
            self.assertIs(client, fake_pg)
            self.assertFalse(as_owner)

    async def test_22_l1_cannot_access_backup(self) -> None:
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.reply_nav import _soft_admin, open_admin_backup_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            cb = SimpleNamespace(data="adm:backup:create", answer=AsyncMock(), message=None)
            with self._admin_ids(), patch(
                "app.services.backup.create_backup",
                side_effect=AssertionError("backup create"),
            ):
                await backup_create(cb, fx.ua, session=session)
            cb.answer.assert_awaited()
            self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_backup_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                await _soft_admin(
                    msg, session, fx.ua, "adm:backup:create", None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_23_l1_cannot_access_settings(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_settings_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_settings_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_24_l1_cannot_access_broadcast(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_broadcast_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_broadcast_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_25_l1_cannot_access_platform_plans(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_plans_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            msg, _ = _fake_msg()
            with self._admin_ids():
                await open_admin_plans_hub(
                    msg, session, fx.ua, MagicMock(), is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_26_l1_cannot_access_pg_admin_management(self) -> None:
        self.assertFalse(is_migrated_pg_soft_callback("adm:pg:admins"))
        src = open("app/bot/handlers/reply_nav.py", encoding="utf-8").read()
        self.assertNotIn("adm:pg:admins", src)
        from app.services.principal_pg_authz import (
            LEVEL1_BLOCKED_PG_PAGES,
            local_safety_allows_pg_page,
        )

        staff = {"role": "principal", "org_depth": 1, "web_owner": False}
        self.assertIn("pg_admins", LEVEL1_BLOCKED_PG_PAGES)
        self.assertFalse(local_safety_allows_pg_page(staff, "pg_admins"))

    async def test_27_l1_cannot_access_platform_pg_overview(self) -> None:
        from app.bot.handlers.admin import pg_stats
        from app.bot.handlers.reply_nav import _soft_admin

        async with self.Session() as session:
            fx = await self._fixtures(session)
            cb = SimpleNamespace(data="adm:pg:stats", answer=AsyncMock(), message=None)
            with self._admin_ids(), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("overview uses owner get_pg"),
            ):
                await pg_stats(cb, fx.ua, session=session)
            cb.answer.assert_awaited()
            self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])
            msg, _ = _fake_msg()
            with self._admin_ids():
                await _soft_admin(
                    msg, session, fx.ua, "adm:pg:stats", None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_28_l2_bot_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            l2_user = BotUser(
                telegram_id=77999,
                role=Role.USER.value,
                referral_code="l24g",
            )
            session.add(l2_user)
            await session.commit()
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=l2_user,
                    action="list",
                    callback_data="adm:pg:users",
                )
            self.assertFalse(gate.allowed)
            from app.bot.handlers.reply_nav import open_pg_home

            msg, _ = _fake_msg()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await open_pg_home(
                    msg, session, l2_user, None, is_reseller_bot=False
                )
            self.assertTrue(msg.answer.await_count)

    async def test_29_shop_bot_pg_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="list",
                    callback_data="adm:pg:users",
                    is_reseller_bot=True,
                    reseller_profile_id=int(fx.pa.id),
                    reseller_owner_id=int(fx.ua.id),
                )
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "shop_bot_isolated")

    async def test_30_phase4b_e_gates_unchanged_owner_still_works(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_users = AsyncMock(return_value={"users": []})
            with self._admin_ids(77001), patch(
                "app.services.pasarguard.get_pg",
                return_value=fake_pg,
            ), patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "features": ["pg_users", "pg_nodes", "pg_templates", "pg_groups"],
                        "pg_is_owner": True,
                        "username": "env_owner",
                        "role": {"is_owner": True},
                    }
                ),
            ):
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.owner_user,
                    action="list",
                    callback_data="adm:pg:users",
                )
                self.assertTrue(gate.allowed, gate.reason)
                self.assertTrue(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )


if __name__ == "__main__":
    unittest.main()
