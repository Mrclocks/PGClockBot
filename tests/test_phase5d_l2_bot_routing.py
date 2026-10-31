"""Phase 5D — L2 Bot routing / reply-nav red team (authorization only)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, ResellerProfile, Role
from app.bot import keyboards as kb
from app.bot.auth import (
    MIGRATED_PG_PAGES,
    OWNER_REQUIRED_MESSAGE,
    bot_migrated_pg_features,
    is_bot_owner_principal,
    is_migrated_pg_soft_callback,
)
from app.services.bot_l2_bind import bind_l2_bot_telegram
from app.services.bot_pg_user_pilot import authorize_bot_pg_user_op
from app.services.bot_principal_identity import (
    bot_pg_client_for_resolution,
    callback_carries_principal_tamper,
    resolve_bot_org_principal,
    resolve_bot_principal_bridge,
)
from app.services.org_principals import (
    attach_org_principal_fields,
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.pg_access import map_pg_role_to_features
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_web_identity import ROLE_PRINCIPAL


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
ALL_PG = {
    **USERS_FULL,
    **NODES_FULL,
    **HOSTS_FULL,
    **CATALOG_FULL,
    "system": {"read": True},
}
OWN_ACCESS = {"allowed_template_ids": [10], "allowed_group_ids": [1]}
APPROVED = frozenset(
    {"pg_users", "pg_nodes", "pg_hosts", "pg_templates", "pg_groups"}
)


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


def _fake_cb(data: str):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        from_user=SimpleNamespace(id=1),
    )


def _fake_state(data=None):
    store = dict(data or {})

    async def _get_data():
        return dict(store)

    async def _update(**kwargs):
        store.update(kwargs)

    async def _set_data(payload):
        store.clear()
        store.update(payload or {})

    return SimpleNamespace(
        get_data=AsyncMock(side_effect=_get_data),
        update_data=AsyncMock(side_effect=_update),
        set_state=AsyncMock(),
        set_data=AsyncMock(side_effect=_set_data),
        clear=AsyncMock(),
        get_state=AsyncMock(return_value=None),
    )


class Phase5DBotL2RoutingTests(unittest.IsolatedAsyncioTestCase):
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
            session, tid=77001, code="own5d", role=Role.ADMIN.value
        )
        ua, pa = await self._shop(session, tid=77010, code="a5d", uname="shopa")
        ub, pb = await self._shop(session, tid=77020, code="b5d", uname="shopb")
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
        l2_user = await self._user(session, tid=77101, code="l2a1")
        sib_user = await self._user(session, tid=77102, code="l2a2")
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
            admin_ids={77001},
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
            admin_ids={77001},
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
        session.add(l2_shop)
        await session.flush()
        a1.reseller_profile_id = int(l2_shop.id)
        a1.bot_user_id = int(l2_user.id)
        await session.commit()
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
            users={
                201: _pg_user(201, "pg_a1"),
                202: _pg_user(202, "pg_shopa"),
                203: _pg_user(203, "pg_a2"),
            },
        )

    def _admin_ids(self, *ids: int):
        return patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(admin_ids=set(ids)),
        )

    def _l2_patches(self, permissions: dict, fake_pg):
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
            self._admin_ids(77001),
        )

    def _l2_kw(self, fx) -> dict:
        return {
            "is_reseller_bot": True,
            "reseller_profile_id": int(fx.l2_shop.id),
            "reseller_owner_id": int(fx.l2_user.id),
        }

    def _enter_l2(self, permissions: dict, fake_pg):
        patches = self._l2_patches(permissions, fake_pg)
        return patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]

    async def test_a_l2_pg_home_only_approved_families(self) -> None:
        from app.bot.handlers.reply_nav import open_pg_home
        from app.bot.keyboards import _pg_submenu_entries

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                feats = await bot_migrated_pg_features(
                    session, fx.l2_user, **self._l2_kw(fx)
                )
                self.assertEqual(feats, APPROVED)
                self.assertEqual(feats, feats & MIGRATED_PG_PAGES)
                self.assertNotIn("pg_overview", feats)
                self.assertNotIn("pg_admins", feats)
                keys = {
                    k for k, _ in _pg_submenu_entries(features=feats, can_create_user=True)
                }
                self.assertNotIn(kb.REPLY_ACTION_PG_STATS, keys)
                self.assertIn(kb.REPLY_ACTION_PG_USERS, keys)
                self.assertIn(kb.REPLY_ACTION_PG_NODES, keys)
                self.assertIn(kb.REPLY_ACTION_PG_GROUP, keys)
                self.assertIn(kb.REPLY_ACTION_PG_TEMPLATE, keys)
                msg, _ = _fake_msg()
                await open_pg_home(
                    msg, session, fx.l2_user, _fake_state(), **self._l2_kw(fx)
                )
            texts = " ".join(
                str(c.args[0]) for c in msg.answer.await_args_list if c.args
            )
            self.assertNotIn("مالک", texts)
            self.assertIn("پاسارگارد", texts)

    async def test_b_l2_cannot_open_pg_overview(self) -> None:
        from app.bot.handlers.admin import pg_stats
        from app.bot.handlers.reply_nav import _soft_admin, reply_main_nav

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                cb = _fake_cb("adm:pg:stats")
                await pg_stats(cb, fx.l2_user, session=session)
                self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])
                msg, bubble = _fake_msg()
                await _soft_admin(
                    msg, session, fx.l2_user, "adm:pg:stats", None, **self._l2_kw(fx)
                )
                deny = msg.answer.await_args.args[0]
                self.assertTrue("مالک" in deny or "ربات اصلی" in deny, deny)
                bubble.edit_text.assert_not_awaited()
                msg2, _ = _fake_msg()
                await reply_main_nav(
                    msg2,
                    session,
                    fx.l2_user,
                    _fake_state(),
                    reply_action=kb.REPLY_ACTION_PG_STATS,
                    reply_ui={},
                    reply_role=Role.USER.value,
                    is_reseller_bot=False,
                )
                self.assertIn("مالک", msg2.answer.await_args.args[0])
        self.assertFalse(is_migrated_pg_soft_callback("adm:pg:stats"))

    async def test_c_l2_cannot_open_backup_settings_plans_broadcast(self) -> None:
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.reply_nav import (
            open_admin_backup_hub,
            open_admin_broadcast_hub,
            open_admin_plans_hub,
            open_admin_settings_hub,
            _soft_admin,
        )

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            msg, _ = _fake_msg()
            with p0, p1, p2, p3, p4, p5:
                await open_admin_backup_hub(
                    msg, session, fx.l2_user, None, **self._l2_kw(fx)
                )
                await open_admin_settings_hub(
                    msg, session, fx.l2_user, None, **self._l2_kw(fx)
                )
                await open_admin_broadcast_hub(
                    msg, session, fx.l2_user, None, **self._l2_kw(fx)
                )
                await open_admin_plans_hub(
                    msg, session, fx.l2_user, _fake_state(), **self._l2_kw(fx)
                )
                await _soft_admin(
                    msg,
                    session,
                    fx.l2_user,
                    "adm:backup:create",
                    None,
                    is_reseller_bot=False,
                )
                cb = _fake_cb("adm:backup:create")
                await backup_create(cb, fx.l2_user, session=session)
            self.assertIn("مالک", msg.answer.await_args.args[0])
            self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])

    async def test_d_l2_cannot_invoke_owner_via_reply_nav(self) -> None:
        from app.bot.handlers.reply_nav import (
            handle_back,
            reply_main_nav,
            _OWNER_ONLY_REPLY_ACTIONS,
        )

        owner_actions = [
            kb.REPLY_ACTION_ADMIN,
            kb.REPLY_ACTION_ADMIN_BACKUP,
            kb.REPLY_ACTION_ADMIN_SETTINGS,
            kb.REPLY_ACTION_ADMIN_BROADCAST,
            kb.REPLY_ACTION_ADMIN_PLANS,
            kb.REPLY_ACTION_ADMIN_USERS,
            kb.REPLY_ACTION_ADMIN_RESELLERS,
            kb.REPLY_ACTION_ADM_PLANS_AUD_USERS,
            kb.REPLY_ACTION_ADM_PLANS_AUD_RESELLERS,
            kb.REPLY_ACTION_ADM_PLANS_ADD,
            kb.REPLY_ACTION_ADM_PLANS_KIND_USERS_FIXED,
            "backup_create",
            "adm_plan_add",
            kb.REPLY_ACTION_ADM_RES_ADD,
        ]
        self.assertIn(kb.REPLY_ACTION_ADM_PLANS_AUD_USERS, _OWNER_ONLY_REPLY_ACTIONS)

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5, patch(
                "app.bot.handlers.admin_plans.send_users_plans_overview",
                new=AsyncMock(side_effect=AssertionError("plans leak")),
            ), patch(
                "app.bot.handlers.admin_plans.send_resellers_plans_overview",
                new=AsyncMock(side_effect=AssertionError("plans leak")),
            ), patch(
                "app.bot.handlers.admin_plans.open_kind_screen",
                new=AsyncMock(side_effect=AssertionError("plans leak")),
            ):
                for action in owner_actions:
                    msg, _ = _fake_msg()
                    await reply_main_nav(
                        msg,
                        session,
                        fx.l2_user,
                        _fake_state(),
                        reply_action=action,
                        reply_ui={},
                        reply_role=Role.USER.value,
                        is_reseller_bot=False,
                    )
                    self.assertTrue(msg.answer.await_count, action)
                    self.assertIn("مالک", msg.answer.await_args.args[0], action)

                msg, _ = _fake_msg()
                with patch(
                    "app.bot.menu_nav.pop_nav_level",
                    new=AsyncMock(return_value="admin_plans_add_type"),
                ):
                    await handle_back(
                        msg,
                        session,
                        fx.l2_user,
                        _fake_state({"_adm_plans_aud": "users"}),
                        is_reseller_bot=False,
                    )
                self.assertIn("مالک", msg.answer.await_args.args[0])

    async def test_e_fabricated_staff_does_not_open_owner_handlers(self) -> None:
        from app.bot.handlers.admin import pg_stats
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.admin_plans import plans_hub
        from app.bot.handlers.admin_settings import settings_hub

        fake_staff = {
            "role": "admin",
            "web_owner": True,
            "org_principal_id": 1,
            "org_depth": 0,
            "pg_is_owner": True,
        }
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                cb = _fake_cb("adm:backup:create")
                await backup_create(
                    cb, fx.l2_user, session=session, staff=fake_staff, web_owner=True
                )
                self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])
                cb2 = _fake_cb("adm:pg:stats")
                await pg_stats(
                    cb2, fx.l2_user, session=session, staff=fake_staff
                )
                self.assertIn(OWNER_REQUIRED_MESSAGE, cb2.answer.await_args.args[0])
                cb3 = _fake_cb("adm:settings")
                await settings_hub(
                    cb3, session, fx.l2_user, _fake_state()
                )
                # Inline back is navigation, not an Owner trap.
                if cb3.answer.await_args and cb3.answer.await_args.args:
                    self.assertNotIn(
                        OWNER_REQUIRED_MESSAGE, cb3.answer.await_args.args[0]
                    )
                cb3.message.edit_text.assert_awaited()
                cb3.message.answer.assert_not_awaited()
                cb4 = _fake_cb("adm:plans")
                await plans_hub(
                    cb4, session, fx.l2_user, _fake_state(), staff=fake_staff
                )
                self.assertIn(OWNER_REQUIRED_MESSAGE, cb4.answer.await_args.args[0])

    async def test_f_role_admin_is_not_owner(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.l2_user.role = Role.ADMIN.value
            await session.commit()
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, fx.l2_user, **self._l2_kw(fx)
                    )
                )
                principal = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
                self.assertIsNotNone(principal)
                self.assertFalse(is_owner_principal(principal))
                self.assertEqual(int(principal.depth), 2)

    async def test_g_web_owner_true_unused_on_l2(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                resolution = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            self.assertIsNotNone(resolution)
            staff = dict(resolution.staff)
            self.assertEqual(staff.get("role"), ROLE_PRINCIPAL)
            self.assertFalse(staff.get("web_owner"))
            self.assertFalse(staff.get("pg_is_owner"))
            self.assertFalse(is_explicit_owner_staff(staff))
            staff["web_owner"] = True
            staff["role"] = "admin"
            self.assertFalse(
                await is_bot_owner_principal(
                    session, fx.l2_user, **self._l2_kw(fx)
                )
            )

    async def test_h_injected_hierarchy_fields_ignored(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(USERS_FULL, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                resolved = await resolve_bot_org_principal(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    spoof_org_principal_id=int(fx.owner_p.id),
                )
                self.assertEqual(int(resolved.id), int(fx.a1.id))
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data=(
                        f"adm:pg:users:org_principal_id={fx.owner_p.id}"
                        f":org_depth=0:parent_id={fx.pa_p.id}:pg_username=env_owner"
                    ),
                )
            self.assertFalse(gate.allowed)
            self.assertTrue(
                callback_carries_principal_tamper(
                    f"adm:pg:users:org_principal:{fx.a2.id}"
                )
            )

    async def test_i_l2_cannot_reach_sibling_or_parent_resources(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_by_id = AsyncMock(
                side_effect=lambda uid: dict(fx.users[int(uid)])
            )
            p0, p1, p2, p3, p4, p5 = self._enter_l2(USERS_FULL, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                sib = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="read",
                    callback_data="adm:pg:u:203",
                )
                parent = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="read",
                    callback_data="adm:pg:u:202",
                )
                own = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="read",
                    callback_data="adm:pg:u:201",
                )
            self.assertFalse(sib.allowed)
            self.assertFalse(parent.allowed)
            self.assertTrue(own.allowed, own.reason)

    async def test_j_k_l2_client_is_own_principal_not_owner_or_l1(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(USERS_FULL, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                resolution = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
                client, as_owner = await bot_pg_client_for_resolution(
                    session, resolution
                )
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data="adm:pg:users",
                )
            self.assertIs(client, fake_pg)
            self.assertFalse(as_owner)
            self.assertIs(gate.pg_client, fake_pg)
            self.assertEqual(resolution.channel, "principal_l2")

    async def test_l_disabled_l1_parent_denies_all_l2_bot(self) -> None:
        from app.bot.handlers.reply_nav import open_pg_home

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.pa_p.status = "disabled"
            await session.commit()
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                feats = await bot_migrated_pg_features(
                    session, fx.l2_user, **self._l2_kw(fx)
                )
                self.assertEqual(feats, frozenset())
                msg, _ = _fake_msg()
                await open_pg_home(
                    msg, session, fx.l2_user, _fake_state(), **self._l2_kw(fx)
                )
                self.assertIn("مالک", msg.answer.await_args.args[0])
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data="adm:pg:users",
                )
            self.assertFalse(gate.allowed)

    async def test_m_disabled_l2_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.a1.status = "disabled"
            await session.commit()
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                self.assertIsNone(
                    await resolve_bot_org_principal(
                        session, db_user=fx.l2_user, **self._l2_kw(fx)
                    )
                )
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data="adm:pg:users",
                )
            self.assertFalse(gate.allowed)

    async def test_n_shop_bot_deny(self) -> None:
        from app.bot.handlers.reply_nav import open_pg_home, _soft_admin

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            p0, p1, p2, p3, p4, p5 = self._enter_l2(ALL_PG, fake_pg)
            with p0, p1, p2, p3, p4, p5:
                msg, _ = _fake_msg()
                await open_pg_home(
                    msg, session, fx.l2_user, _fake_state(), is_reseller_bot=True
                )
                self.assertIn("ربات اصلی", msg.answer.await_args.args[0])
                msg2, _ = _fake_msg()
                await _soft_admin(
                    msg2,
                    session,
                    fx.l2_user,
                    "adm:pg:users",
                    None,
                    is_reseller_bot=True,
                )
                self.assertIn("ربات اصلی", msg2.answer.await_args.args[0])
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

    async def test_o_owner_behavior_unchanged(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_backup_hub, reply_main_nav

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
            with self._admin_ids(77001), patch(
                "app.services.pasarguard.get_pg",
                return_value=fake_pg,
            ), patch(
                "app.services.backup.list_backups",
                return_value=[],
            ), patch(
                "app.bot.handlers.admin_plans.send_users_plans_overview",
                new=AsyncMock(),
            ) as plans_fn:
                self.assertTrue(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )
                msg, _ = _fake_msg()
                await open_admin_backup_hub(
                    msg, session, fx.owner_user, _fake_state(), is_reseller_bot=False
                )
                self.assertIn("بکاپ", msg.answer.await_args_list[0].args[0])
                msg2, _ = _fake_msg()
                await reply_main_nav(
                    msg2,
                    session,
                    fx.owner_user,
                    _fake_state(),
                    reply_action=kb.REPLY_ACTION_ADM_PLANS_AUD_USERS,
                    reply_ui={},
                    reply_role=Role.ADMIN.value,
                    is_reseller_bot=False,
                )
                plans_fn.assert_not_awaited()
                self.assertTrue(msg2.answer.await_count)

    async def test_p_l1_behavior_unchanged(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_backup_hub, open_pg_home

        async with self.Session() as session:
            fx = await self._fixtures(session)
            fake_pg = AsyncMock()
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
            ), self._admin_ids(77001):
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.ua,
                    action="list",
                    callback_data="adm:pg:users",
                )
                self.assertTrue(gate.allowed, gate.reason)
                self.assertIs(gate.pg_client, fake_pg)
                msg, _ = _fake_msg()
                await open_admin_backup_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                self.assertIn("مالک", msg.answer.await_args.args[0])
                msg2, _ = _fake_msg()
                await open_pg_home(
                    msg2, session, fx.ua, _fake_state(), is_reseller_bot=False
                )
            texts = " ".join(
                str(c.args[0]) for c in msg2.answer.await_args_list if c.args
            )
            self.assertIn("پاسارگارد", texts)
            self.assertNotIn("مالک", texts)


if __name__ == "__main__":
    unittest.main()
