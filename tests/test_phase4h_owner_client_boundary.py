"""Phase 4H — remaining Owner get_pg() boundary + disabled-Owner auth."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
from app.bot.auth import (
    OWNER_REQUIRED_MESSAGE,
    is_bot_owner_principal,
    is_migrated_pg_soft_callback,
)
from app.services.bot_pg_catalog_pilot import authorize_bot_pg_catalog_op
from app.services.bot_pg_object_pilot import authorize_bot_pg_object_op
from app.services.bot_pg_user_pilot import authorize_bot_pg_user_op
from app.services.bot_principal_identity import (
    bot_pg_client_for_resolution,
    resolve_bot_org_principal,
    resolve_bot_principal_bridge,
)
from app.services.org_principals import (
    OrgPrincipalError,
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
    get_active_owner,
    resolve_org_principal_for_staff,
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


class Phase4HOwnerClientBoundaryTests(unittest.IsolatedAsyncioTestCase):
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
            telegram_id=88001,
            role=Role.ADMIN.value,
            referral_code="own4h",
        )
        session.add(owner_user)
        sticky = BotUser(
            telegram_id=88002,
            role=Role.ADMIN.value,
            referral_code="stk4h",
        )
        session.add(sticky)
        ua, pa = await self._shop(session, tid=88010, code="a4h", uname="shopa")
        ub, pb = await self._shop(session, tid=88020, code="b4h", uname="shopb")
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
            self._admin_ids(88001),
        )

    def _assert_owner_denied(self, cb) -> None:
        cb.answer.assert_awaited()
        self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])

    # ----- 1 / 6 remaining Owner-only handlers -----

    async def test_1_l1_cannot_reach_owner_only_handlers(self) -> None:
        from app.bot.handlers.admin import adm_resellers_add, adm_resellers_services
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.admin_plans import trial_pick_tpl
        from app.bot.handlers.admin_settings import settings_sub
        from app.bot.handlers.reply_nav import (
            _soft_admin,
            open_admin_backup_hub,
            open_admin_broadcast_hub,
            open_admin_plans_hub,
            open_admin_settings_hub,
        )

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)

            cb = SimpleNamespace(data="adm:backup:create", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await backup_create(cb, fx.ua, session=session)
            self._assert_owner_denied(cb)

            cb = SimpleNamespace(data="adm:plans:trial:tpl", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await trial_pick_tpl(cb, fx.ua, session=session)
            self._assert_owner_denied(cb)

            cb = SimpleNamespace(data="adm:st:sub:service:custom", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await settings_sub(cb, session, fx.ua)
            self._assert_owner_denied(cb)

            cb = SimpleNamespace(data="adm:resellers:add", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await adm_resellers_add(
                    cb, MagicMock(), fx.ua, session=session
                )
            self._assert_owner_denied(cb)

            cb = SimpleNamespace(
                data=f"adm:resellers:svcs:{fx.ua.id}",
                answer=AsyncMock(),
                message=None,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await adm_resellers_services(cb, session, fx.ua)
            self._assert_owner_denied(cb)

            msg, _ = _fake_msg()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await open_admin_backup_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                await open_admin_settings_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                await open_admin_broadcast_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                await open_admin_plans_hub(
                    msg, session, fx.ua, MagicMock(), is_reseller_bot=False
                )
                await _soft_admin(
                    msg, session, fx.ua, "adm:backup:create", None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

        self.assertFalse(is_migrated_pg_soft_callback("adm:backup"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:settings"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:plans"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:resellers:add"))

    # ----- 2 L1 never Owner get_pg -----

    async def test_2_l1_never_receives_owner_get_pg(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = object()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                resolution = await resolve_bot_principal_bridge(
                    session, db_user=fx.ua, is_reseller_bot=False
                )
                self.assertIsNotNone(resolution)
                client, as_owner = await bot_pg_client_for_resolution(session, resolution)
            self.assertIs(client, fake_pg)
            self.assertFalse(as_owner)

    # ----- 3 platform PG overview -----

    async def test_3_l1_cannot_reach_platform_pg_overview(self) -> None:
        from app.bot.handlers.admin import pg_stats
        from app.bot.handlers.reply_nav import _soft_admin

        async with self.Session() as session:
            fx = await self._fixtures(session)
            cb = SimpleNamespace(data="adm:pg:stats", answer=AsyncMock(), message=None)
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await pg_stats(cb, fx.ua, session=session)
            self._assert_owner_denied(cb)
            msg, _ = _fake_msg()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await _soft_admin(
                    msg, session, fx.ua, "adm:pg:stats", None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])
        self.assertFalse(is_migrated_pg_soft_callback("adm:pg:stats"))

    # ----- 4 platform plan / role picker -----

    async def test_4_l1_cannot_reach_platform_plan_role_picker(self) -> None:
        from app.bot.handlers.admin_plans import trial_pick_grp, trial_pick_tpl
        from app.bot.handlers.reply_nav import _soft_admin, open_admin_plans_hub

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches({**USERS_FULL, **CATALOG_FULL}, fake_pg)
            cb = SimpleNamespace(data="adm:plans:trial:tpl", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await trial_pick_tpl(cb, fx.ua, session=session)
            self._assert_owner_denied(cb)

            cb = SimpleNamespace(data="adm:plans:trial:grp", answer=AsyncMock(), message=None)
            state = SimpleNamespace(update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await trial_pick_grp(cb, state, session, fx.ua)
            self._assert_owner_denied(cb)

            msg, _ = _fake_msg()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await open_admin_plans_hub(
                    msg, session, fx.ua, MagicMock(), is_reseller_bot=False
                )
                await _soft_admin(
                    msg, session, fx.ua, "adm:plans", None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

        self.assertFalse(is_migrated_pg_soft_callback("adm:plans:trial:tpl"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:plans:trial:grp"))
        self.assertFalse(is_migrated_pg_soft_callback("adm:resplan:add:setrole"))

    # ----- 5 reseller provisioning -----

    async def test_5_l1_cannot_reach_reseller_provisioning(self) -> None:
        from app.bot.handlers.admin import adm_resellers_add, make_res
        from app.bot.handlers.reply_nav import _soft_admin

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            cb = SimpleNamespace(data="adm:resellers:add", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "app.services.resellers.provision_reseller",
                side_effect=AssertionError("provision must not run"),
            ):
                await adm_resellers_add(cb, MagicMock(), fx.ua, session=session)
            self._assert_owner_denied(cb)

            msg, _ = _fake_msg()
            msg.text = "123456789 15"
            state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "app.services.resellers.provision_reseller",
                side_effect=AssertionError("provision must not run"),
            ):
                await make_res(msg, state, session, fx.ua)
            self.assertTrue(msg.answer.await_count)
            self.assertIn("مالک", msg.answer.await_args.args[0])

            msg, _ = _fake_msg()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await _soft_admin(
                    msg, session, fx.ua, "adm:resellers:add", None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    # ----- 6 backup / settings / broadcast (explicit) -----

    async def test_6_l1_cannot_reach_backup_settings_broadcast(self) -> None:
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.reply_nav import (
            open_admin_backup_hub,
            open_admin_broadcast_hub,
            open_admin_settings_hub,
        )

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            patches = self._l1_patches(USERS_FULL, fake_pg)
            cb = SimpleNamespace(data="adm:backup:create", answer=AsyncMock(), message=None)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "app.services.backup.create_backup",
                side_effect=AssertionError("backup create"),
            ):
                await backup_create(cb, fx.ua, session=session)
            self._assert_owner_denied(cb)

            msg, _ = _fake_msg()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                await open_admin_backup_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                await open_admin_settings_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                await open_admin_broadcast_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
            self.assertIn("مالک", msg.answer.await_args.args[0])

    # ----- 7 Owner still Owner get_pg -----

    async def test_7_owner_still_receives_owner_get_pg(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            owner_pg = object()
            with self._admin_ids(88001), patch(
                "app.services.pasarguard.get_pg",
                return_value=owner_pg,
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                side_effect=AssertionError("Owner must not use reseller client"),
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
                resolution = await resolve_bot_principal_bridge(
                    session, db_user=fx.owner_user, is_reseller_bot=False
                )
                self.assertIsNotNone(resolution)
                client, as_owner = await bot_pg_client_for_resolution(session, resolution)
            self.assertIs(client, owner_pg)
            self.assertTrue(as_owner)
            with self._admin_ids(88001):
                self.assertTrue(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )

    async def test_7b_web_owner_same_org_principal_as_bot(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            web = await resolve_org_principal_for_staff(
                session, {"role": "admin", "web_owner": True}
            )
            with self._admin_ids(88001):
                bot = await resolve_bot_org_principal(
                    session, db_user=fx.owner_user, is_reseller_bot=False
                )
            self.assertIsNotNone(web)
            self.assertIsNotNone(bot)
            assert web is not None and bot is not None
            self.assertEqual(int(web.id), int(bot.id))
            self.assertEqual(int(web.id), int(fx.owner_p.id))

    # ----- 8 migrated families still own client -----

    async def test_8_l1_migrated_pg_paths_use_own_client(self) -> None:
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
            fake_pg.get_nodes = AsyncMock(return_value=[])
            fake_pg.get_user_templates_simple = AsyncMock(
                return_value=[{"id": 10, "name": "tpl_a"}]
            )
            fake_pg.get_groups_simple = AsyncMock(
                return_value=[{"id": 1, "name": "grp_a"}]
            )
            perms = {**USERS_FULL, **NODES_FULL, **CATALOG_FULL}
            patches = self._l1_patches(perms, fake_pg)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                ugate = await authorize_bot_pg_user_op(
                    session, db_user=fx.ua, action="list", callback_data="adm:pg:users"
                )
                ngate = await authorize_bot_pg_object_op(
                    session,
                    db_user=fx.ua,
                    kind="nodes",
                    action="list",
                    callback_data="adm:pg:nodes",
                )
                tgate = await authorize_bot_pg_catalog_op(
                    session,
                    db_user=fx.ua,
                    kind="templates",
                    action="list",
                    callback_data="adm:pg:template",
                )
            self.assertTrue(ugate.allowed, ugate.reason)
            self.assertIs(ugate.pg_client, fake_pg)
            self.assertTrue(ngate.allowed, ngate.reason)
            self.assertIs(ngate.pg_client, fake_pg)
            self.assertTrue(tgate.allowed, tgate.reason)
            self.assertIs(tgate.pg_client, fake_pg)

    # ----- 9 disabled Owner cannot reactivate -----

    async def test_9_disabled_owner_stays_disabled_on_normal_auth(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.owner_p.status = "disabled"
            await session.commit()

            with self.assertRaises(OrgPrincipalError) as ctx:
                await ensure_owner_principal(session)
            self.assertIn("not active", str(ctx.exception).lower())

            await session.refresh(fx.owner_p)
            self.assertEqual(fx.owner_p.status, "disabled")
            self.assertIsNone(await get_active_owner(session))

            owners = list(
                (
                    await session.execute(
                        select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(owners), 1)

            with self._admin_ids(88001):
                self.assertIsNone(
                    await resolve_bot_org_principal(
                        session, db_user=fx.owner_user, is_reseller_bot=False
                    )
                )
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )

            web = await resolve_org_principal_for_staff(
                session, {"role": "admin", "web_owner": True}
            )
            self.assertIsNone(web)

            await session.refresh(fx.owner_p)
            self.assertEqual(fx.owner_p.status, "disabled")
            self.assertIsNone(await get_active_owner(session))
            owners = list(
                (
                    await session.execute(
                        select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(owners), 1)

    # ----- 10 no second Owner -----

    async def test_10_no_second_owner_created(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=None, depth=0)

            fx.owner_p.status = "disabled"
            await session.commit()
            with self.assertRaises(OrgPrincipalError):
                await create_principal(
                    session, parent_id=None, depth=0, status="active"
                )
            with self.assertRaises(OrgPrincipalError):
                await ensure_owner_principal(session)

            owners = list(
                (
                    await session.execute(
                        select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0].status, "disabled")

    async def test_10b_bootstrap_create_only_when_zero_owners(self) -> None:
        async with self.Session() as session:
            created = await ensure_owner_principal(session)
            again = await ensure_owner_principal(session)
            self.assertEqual(int(created.id), int(again.id))
            self.assertEqual(created.status, "active")

    # ----- 11 shop bot isolated -----

    async def test_11_shop_bot_remains_isolated(self) -> None:
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

    # ----- 12 L2 Bot DENY -----

    async def test_12_l2_bot_remains_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            l2_user = BotUser(
                telegram_id=88999,
                role=Role.USER.value,
                referral_code="l24h",
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


if __name__ == "__main__":
    unittest.main()
