"""Phase 5E — final Owner → L1 → L2 hierarchy red team.

Attack matrix A–AJ. Expect DENY / fail-closed. A pass is not a feature.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, ResellerProfile, Role
from app.bot.auth import OWNER_REQUIRED_MESSAGE, is_bot_owner_principal
from app.services.authz import (
    authz_from_staff,
    authorize,
    has_org_global_scope,
    is_explicit_org_owner,
)
from app.services.bot_l2_bind import L2BotBindError, bind_l2_bot_telegram
from app.services.bot_pg_user_pilot import authorize_bot_pg_user_op
from app.services.bot_principal_identity import (
    bot_pg_client_for_resolution,
    resolve_bot_org_principal,
    resolve_bot_principal_bridge,
)
from app.services.org_principals import (
    OrgPrincipalError,
    attach_org_principal_fields,
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.pasarguard import PasarGuardError
from app.services.pg_access import map_pg_role_to_features
from app.services.pg_user_scope import pg_user_in_staff_scope
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    Level2ProvisionRequest,
    provision_level2_child,
)
from app.services.principal_pg_authz import authorize_pg_page
from app.services.principal_provisioning import (
    Level1ProvisionRequest,
    PrincipalProvisionError,
    provision_level1_principal,
)
from app.services.principal_web_identity import (
    PrincipalWebIdentityError,
    ROLE_PRINCIPAL,
    attach_level1_web_identity,
    attach_level2_web_identity,
    authenticate_level1_web,
    build_principal_session_payload,
    resolve_principal_web_session,
)
from app.services.secret_box import encrypt_secret
from tests.postgres_support import scoped_async_engine


OWNER_TID = 88001
_WEB = "WebRed12!@Ab"
USERS_FULL = {
    "users": {
        "read": True,
        "read_simple": True,
        "create": True,
        "update": True,
        "delete": True,
    }
}
OWN_ACCESS = {"allowed_template_ids": [10], "allowed_group_ids": [1]}


def _role(permissions: dict, *, access: dict | None = OWN_ACCESS) -> dict:
    out = {"id": 10, "name": "Administrator", "is_owner": False, "permissions": permissions}
    if access is not None:
        out["access"] = access
    return out


def _pg_user(uid: int, admin: str | None) -> dict:
    row: dict = {"id": uid, "username": f"vpn_{uid}"}
    if admin:
        row["admin"] = {"username": admin}
    return row


def _fake_msg():
    bubble = SimpleNamespace(edit_text=AsyncMock(), from_user=SimpleNamespace(id=1), bot=SimpleNamespace())
    msg = SimpleNamespace(
        answer=AsyncMock(return_value=bubble),
        from_user=SimpleNamespace(id=1),
        bot=SimpleNamespace(),
        text="x",
        edit_text=AsyncMock(),
    )
    return msg, bubble


class Phase5EFinalHierarchyRedTeam(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _admin_ids(self, *ids: int):
        return patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(admin_ids=set(ids) or {OWNER_TID}),
        )

    def _pg_live(self, permissions: dict | None = None):
        role = _role(permissions or USERS_FULL)
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
        )

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
            pg_admin_password_enc=encrypt_secret(f"Pw_{uname}_12!@"),
            pg_role_id=10,
        )
        session.add(profile)
        await session.flush()
        return user, profile

    async def _tree(self, session):
        owner_p = await ensure_owner_principal(session)
        owner_user = await self._user(
            session, tid=OWNER_TID, code="own5e", role=Role.ADMIN.value
        )
        ua, pa = await self._shop(session, tid=88010, code="a5e", uname="shopa")
        ub, pb = await self._shop(session, tid=88020, code="b5e", uname="shopb")
        l1a = await bind_reseller_profile_principal(session, pa)
        l1b = await bind_reseller_profile_principal(session, pb)
        a1 = await create_principal(
            session,
            parent_id=int(l1a.id),
            depth=2,
            pg_username="pg_a1",
            pg_password_enc=encrypt_secret("ChildA112!@yy"),
        )
        a2 = await create_principal(
            session,
            parent_id=int(l1a.id),
            depth=2,
            pg_username="pg_a2",
            pg_password_enc=encrypt_secret("ChildA234!@zz"),
        )
        b1 = await create_principal(
            session,
            parent_id=int(l1b.id),
            depth=2,
            pg_username="pg_b1",
            pg_password_enc=encrypt_secret("ChildB178!@bb"),
        )
        l2_a1_user = await self._user(session, tid=88101, code="l2a1")
        l2_a2_user = await self._user(session, tid=88102, code="l2a2")
        l2_b1_user = await self._user(session, tid=88121, code="l2b1")
        l1_staff_a = attach_org_principal_fields(
            {
                "role": "reseller",
                "bot_user_id": int(ua.id),
                "reseller_profile_id": int(pa.id),
            },
            l1a,
        )
        shops: dict[int, ResellerProfile] = {}
        for child, tg in (
            (a1, l2_a1_user),
            (a2, l2_a2_user),
            (b1, l2_b1_user),
        ):
            parent = l1a if int(child.parent_id) == int(l1a.id) else l1b
            parent_user = ua if parent is l1a else ub
            parent_prof = pa if parent is l1a else pb
            staff = attach_org_principal_fields(
                {
                    "role": "reseller",
                    "bot_user_id": int(parent_user.id),
                    "reseller_profile_id": int(parent_prof.id),
                },
                parent,
            )
            await bind_l2_bot_telegram(
                session,
                staff=staff,
                target_principal_id=int(child.id),
                telegram_id=int(tg.telegram_id),
                admin_ids={OWNER_TID},
            )
            shop = ResellerProfile(
                user_id=int(tg.id),
                is_active=True,
                web_username=f"shop_{child.pg_username}",
                web_password_hash="x" * 24,
                setup_completed_at=datetime.now(timezone.utc),
                pg_admin_username=child.pg_username,
                pg_role_id=10,
            )
            session.add(shop)
            await session.flush()
            child.reseller_profile_id = int(shop.id)
            child.bot_user_id = int(tg.id)
            shops[int(tg.id)] = shop
        ident_l1a = await attach_level1_web_identity(
            session, principal_id=int(l1a.id), web_username="pg_shopa", password=_WEB
        )
        ident_a1 = await attach_level2_web_identity(
            session, principal_id=int(a1.id), web_username="pg_a1", password=_WEB
        )
        ident_a2 = await attach_level2_web_identity(
            session, principal_id=int(a2.id), web_username="pg_a2", password=_WEB
        )
        ident_b1 = await attach_level2_web_identity(
            session, principal_id=int(b1.id), web_username="pg_b1", password=_WEB
        )
        await session.commit()
        return SimpleNamespace(
            owner_p=owner_p,
            owner_user=owner_user,
            ua=ua,
            pa=pa,
            l1a=l1a,
            ub=ub,
            pb=pb,
            l1b=l1b,
            a1=a1,
            a2=a2,
            b1=b1,
            l2_a1_user=l2_a1_user,
            l2_a2_user=l2_a2_user,
            l2_b1_user=l2_b1_user,
            l2_a1_shop=shops[int(l2_a1_user.id)],
            l2_a2_shop=shops[int(l2_a2_user.id)],
            l2_b1_shop=shops[int(l2_b1_user.id)],
            ident_l1a=ident_l1a,
            ident_a1=ident_a1,
            ident_a2=ident_a2,
            ident_b1=ident_b1,
            l1_staff_a=l1_staff_a,
        )

    async def _web(self, session, username: str):
        auth = await authenticate_level1_web(session, username=username, password=_WEB)
        assert auth is not None
        live, feats = self._pg_live()
        with live, feats:
            return await resolve_principal_web_session(
                session, build_principal_session_payload(auth)
            )

    def _l2_kw(self, fx, db_user=None) -> dict:
        user = db_user or fx.l2_a1_user
        shop = fx.l2_a1_shop
        if int(user.id) == int(fx.l2_a2_user.id):
            shop = fx.l2_a2_shop
        elif int(user.id) == int(fx.l2_b1_user.id):
            shop = fx.l2_b1_shop
        return {
            "is_reseller_bot": True,
            "reseller_profile_id": int(shop.id),
            "reseller_owner_id": int(user.id),
        }

    async def _bot_bridge(self, session, db_user, fake_pg=None, fx=None):
        live, feats = self._pg_live()
        pg = fake_pg if fake_pg is not None else AsyncMock()
        kw = {"is_reseller_bot": False}
        if fx is not None:
            kw = self._l2_kw(fx, db_user)
        with live, feats, self._admin_ids(OWNER_TID), patch(
            "app.services.pasarguard.get_pg",
            side_effect=AssertionError("must not use Owner get_pg()"),
        ), patch(
            "app.services.pasarguard.get_pg_for_reseller",
            new=AsyncMock(return_value=pg),
        ), patch(
            "app.services.pasarguard.get_pg_for_principal",
            new=AsyncMock(return_value=pg),
        ):
            return await resolve_bot_principal_bridge(
                session, db_user=db_user, **kw
            )

    async def test_invariants_owner_unique_and_depth(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            self.assertTrue(is_owner_principal(fx.owner_p))
            self.assertEqual(int(fx.l1a.parent_id), int(fx.owner_p.id))
            self.assertEqual(int(fx.l1b.parent_id), int(fx.owner_p.id))
            self.assertEqual(int(fx.a1.parent_id), int(fx.l1a.id))
            self.assertEqual(int(fx.b1.parent_id), int(fx.l1b.id))
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=int(fx.a1.id), depth=3)
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=None, depth=0)
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=int(fx.owner_p.id), depth=2)

    async def test_a_to_g_scope_isolation(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            vis_l1a = await visible_principal_ids(session, fx.l1a)
            vis_l1b = await visible_principal_ids(session, fx.l1b)
            vis_a1 = await visible_principal_ids(session, fx.a1)
            vis_a2 = await visible_principal_ids(session, fx.a2)
            vis_b1 = await visible_principal_ids(session, fx.b1)
            vis_own = await visible_principal_ids(session, fx.owner_p)
            # A L1A ↛ L1B
            self.assertNotIn(int(fx.l1b.id), vis_l1a)
            # B L1A ↛ L2 B1
            self.assertNotIn(int(fx.b1.id), vis_l1a)
            # C L2 A1 ↛ L1 A
            self.assertNotIn(int(fx.l1a.id), vis_a1)
            # D L2 A1 ↛ L2 A2
            self.assertNotIn(int(fx.a2.id), vis_a1)
            # E L2 A1 ↛ L2 B1
            self.assertNotIn(int(fx.b1.id), vis_a1)
            # F L2 A1 ↛ Owner
            self.assertNotIn(int(fx.owner_p.id), vis_a1)
            # G L1 A ↛ Owner
            self.assertNotIn(int(fx.owner_p.id), vis_l1a)
            self.assertEqual(vis_a1, frozenset({int(fx.a1.id)}))
            self.assertEqual(vis_a2, frozenset({int(fx.a2.id)}))
            self.assertEqual(vis_b1, frozenset({int(fx.b1.id)}))
            self.assertEqual(vis_l1a, frozenset({int(fx.l1a.id), int(fx.a1.id), int(fx.a2.id)}))
            self.assertEqual(vis_l1b, frozenset({int(fx.l1b.id), int(fx.b1.id)}))
            self.assertTrue(
                {int(fx.owner_p.id), int(fx.l1a.id), int(fx.l1b.id), int(fx.a1.id), int(fx.b1.id)}.issubset(
                    vis_own
                )
            )

    async def test_h_to_n_injection_ignored(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            live, feats = self._pg_live()
            cookie = build_principal_session_payload(
                await authenticate_level1_web(session, username="pg_a1", password=_WEB)
            )
            cookie.update(
                {
                    "role": "admin",
                    "web_owner": True,
                    "org_principal_id": int(fx.owner_p.id),
                    "org_parent_id": None,
                    "org_depth": 0,
                    "pg_username": "env_owner",
                    "pg_admin_username": "env_owner",
                    "pg_role_id": 1,
                    "pg_is_owner": True,
                }
            )
            # role=admin is not a Principal session — resolve refuses.
            with live, feats:
                with self.assertRaises(PrincipalWebIdentityError):
                    await resolve_principal_web_session(session, cookie)
            cookie["role"] = ROLE_PRINCIPAL
            with live, feats:
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertIn(ctx.exception.code, {"principal_tamper", "hierarchy_tamper"})
            good = build_principal_session_payload(
                await authenticate_level1_web(session, username="pg_a1", password=_WEB)
            )
            good["pg_role_id"] = 1
            good["web_owner"] = True
            good["pg_is_owner"] = True
            with live, feats:
                staff = await resolve_principal_web_session(session, good)
            self.assertEqual(int(staff["org_principal_id"]), int(fx.a1.id))
            self.assertEqual(int(staff["org_depth"]), 2)
            self.assertFalse(staff.get("web_owner"))
            self.assertFalse(staff.get("pg_is_owner"))
            self.assertEqual(staff.get("role"), ROLE_PRINCIPAL)
            self.assertFalse(is_explicit_owner_staff(staff))
            fx.l2_a1_user.role = Role.ADMIN.value
            await session.commit()
            with self._admin_ids(OWNER_TID):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, fx.l2_a1_user, **self._l2_kw(fx)
                    )
                )
                bot = await resolve_bot_org_principal(
                    session, db_user=fx.l2_a1_user, **self._l2_kw(fx)
                )
            self.assertEqual(int(bot.id), int(fx.a1.id))

    async def test_o_p_callback_and_reply_nav(self) -> None:
        from app.bot.handlers.reply_nav import open_admin_backup_hub, reply_main_nav
        from app.bot import keyboards as kb

        async with self.Session() as session:
            fx = await self._tree(session)
            fake_pg = AsyncMock()
            fake_pg.get_user_by_id = AsyncMock(return_value=_pg_user(202, "pg_shopa"))
            live, feats = self._pg_live()
            with live, feats, self._admin_ids(OWNER_TID), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("L2 must not use Owner get_pg()"),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_pg),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake_pg),
            ):
                parent = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_a1_user,
                    **self._l2_kw(fx),
                    action="read",
                    callback_data="adm:pg:u:202",
                )
                tamper = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_a1_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data="adm:pg:users:org_principal_id:1:pg_username:env_owner",
                )
                msg, _ = _fake_msg()
                await open_admin_backup_hub(
                    msg, session, fx.l2_a1_user, None, **self._l2_kw(fx)
                )
                state = SimpleNamespace(
                    get_data=AsyncMock(return_value={}),
                    update_data=AsyncMock(),
                    set_state=AsyncMock(),
                    set_data=AsyncMock(),
                    clear=AsyncMock(),
                    get_state=AsyncMock(return_value=None),
                )
                msg2, _ = _fake_msg()
                await reply_main_nav(
                    msg2,
                    session,
                    fx.l2_a1_user,
                    state,
                    reply_action=kb.REPLY_ACTION_ADM_PLANS_AUD_USERS,
                    reply_ui={},
                    reply_role=Role.USER.value,
                    is_reseller_bot=False,
                )
            self.assertFalse(parent.allowed)
            self.assertFalse(tamper.allowed)
            backup_deny = msg.answer.await_args.args[0]
            self.assertTrue("مالک" in backup_deny or "ربات اصلی" in backup_deny, backup_deny)
            self.assertIn("مالک", msg2.answer.await_args.args[0])

    async def test_q_r_stale_after_disable(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            payload = build_principal_session_payload(
                await authenticate_level1_web(session, username="pg_a1", password=_WEB)
            )
            fx.a1.status = "disabled"
            await session.commit()
            live, feats = self._pg_live()
            with live, feats:
                with self.assertRaises(PrincipalWebIdentityError):
                    await resolve_principal_web_session(session, payload)
            with self._admin_ids(OWNER_TID):
                self.assertIsNone(
                    await resolve_bot_org_principal(
                        session, db_user=fx.l2_a1_user, **self._l2_kw(fx)
                    )
                )
            fx.a1.status = "active"
            fx.l1a.status = "disabled"
            await session.commit()
            with live, feats:
                with self.assertRaises(PrincipalWebIdentityError):
                    await resolve_principal_web_session(session, payload)
            vis = await visible_principal_ids(session, fx.a1)
            self.assertEqual(vis, frozenset())
            with self._admin_ids(OWNER_TID):
                self.assertIsNone(
                    await resolve_bot_org_principal(
                        session, db_user=fx.l2_a1_user, **self._l2_kw(fx)
                    )
                )

    async def test_s_t_u_v_pg_fail_closed(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            payload = build_principal_session_payload(
                await authenticate_level1_web(session, username="pg_a1", password=_WEB)
            )
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                staff = await resolve_principal_web_session(session, payload)
            self.assertFalse(staff.get("pg_capabilities_ok"))
            self.assertFalse(authorize_pg_page(staff, "pg_users").allowed)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                side_effect=PasarGuardError("pg down"),
            ), self._admin_ids(OWNER_TID):
                staff2 = await resolve_principal_web_session(session, payload)
                self.assertFalse(staff2.get("pg_capabilities_ok"))
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_a1_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data="adm:pg:users",
                )
            self.assertFalse(gate.allowed)
            fx.a1.pg_username = None
            fx.a1.pg_password_enc = None
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError):
                await resolve_principal_web_session(session, payload)
            staff_l2 = attach_org_principal_fields(
                {
                    "role": ROLE_PRINCIPAL,
                    "pg_admin_username": "pg_a2",
                    "pg_is_owner": False,
                },
                fx.a2,
                visible_principal_ids=frozenset({int(fx.a2.id)}),
            )
            self.assertFalse(pg_user_in_staff_scope(_pg_user(99, None), staff_l2))
            self.assertFalse(pg_user_in_staff_scope(_pg_user(201, "pg_a1"), staff_l2))
            self.assertFalse(pg_user_in_staff_scope(_pg_user(202, "pg_shopa"), staff_l2))

    async def test_w_x_y_identity_uniqueness(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            with self.assertRaises(L2BotBindError) as dup:
                await bind_l2_bot_telegram(
                    session,
                    staff=attach_org_principal_fields(
                        {
                            "role": "reseller",
                            "bot_user_id": int(fx.ua.id),
                            "reseller_profile_id": int(fx.pa.id),
                        },
                        fx.l1a,
                    ),
                    target_principal_id=int(fx.a2.id),
                    telegram_id=int(fx.l2_a1_user.telegram_id),
                    admin_ids={OWNER_TID},
                )
            self.assertIn(dup.exception.code, {"duplicate_bot_user_id", "binding_collision", "reseller_collision"})
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=None, depth=0)
            fx.owner_p.status = "disabled"
            await session.commit()
            with self.assertRaises(OrgPrincipalError):
                await ensure_owner_principal(session)
            with self._admin_ids(OWNER_TID):
                self.assertFalse(
                    await is_bot_owner_principal(
                        session, fx.owner_user, is_reseller_bot=False
                    )
                )

    async def test_z_aa_ab_provisioning_denied(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            live, feats = self._pg_live()
            with live, feats:
                staff_a1 = await self._web(session, "pg_a1")
                staff_l1a = await self._web(session, "pg_shopa")
            req_l2 = Level2ProvisionRequest(
                pg_username="evil_l2",
                pg_password="EvilL2pw12!@",
                pg_role_id=10,
                idempotency_key="k-l2",
                parent_id=int(fx.l1b.id),
                depth=1,
            )
            with self.assertRaises(ChildProvisionError) as z:
                await provision_level2_child(session, staff_a1, req_l2)
            self.assertEqual(z.exception.code, "not_level1")
            with self.assertRaises(ChildProvisionError):
                await provision_level2_child(session, staff_l1a, req_l2)
            req_l1 = Level1ProvisionRequest(
                pg_username="evil_l1",
                pg_password="EvilL1pw12!@",
                pg_role_id=10,
                idempotency_key="k-l1",
                parent_id=int(fx.owner_p.id),
                depth=0,
            )
            with self.assertRaises(PrincipalProvisionError) as ab:
                await provision_level1_principal(session, staff_a1, req_l1)
            self.assertEqual(ab.exception.code, "not_owner")
            with self.assertRaises(PrincipalProvisionError):
                await provision_level1_principal(session, staff_l1a, req_l1)

    async def test_ac_ad_ae_owner_surfaces_and_shop_bot(self) -> None:
        from app.bot.handlers.admin_backup import backup_create
        from app.bot.handlers.reply_nav import open_admin_backup_hub, open_pg_home

        async with self.Session() as session:
            fx = await self._tree(session)
            live, feats = self._pg_live()
            with live, feats, self._admin_ids(OWNER_TID), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("must not use Owner get_pg()"),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(),
            ):
                msg, _ = _fake_msg()
                await open_admin_backup_hub(
                    msg, session, fx.ua, None, is_reseller_bot=False
                )
                self.assertIn("مالک", msg.answer.await_args.args[0])
                msg2, _ = _fake_msg()
                await open_admin_backup_hub(
                    msg2, session, fx.l2_a1_user, None, is_reseller_bot=False
                )
                self.assertIn("مالک", msg2.answer.await_args.args[0])
                cb = SimpleNamespace(
                    data="adm:backup:create",
                    answer=AsyncMock(),
                    message=None,
                    from_user=SimpleNamespace(id=1),
                )
                await backup_create(cb, fx.l2_a1_user, session=session)
                self.assertIn(OWNER_REQUIRED_MESSAGE, cb.answer.await_args.args[0])
                msg3, _ = _fake_msg()
                await open_pg_home(
                    msg3, session, fx.l2_a1_user, None, **self._l2_kw(fx)
                )
                texts = " ".join(
                    str(c.args[0]) for c in msg3.answer.await_args_list if c.args
                )
                self.assertNotIn("ربات اصلی", texts)
                self.assertIn("پاسارگارد", texts)
                shop_as_l2_chatter = await resolve_bot_org_principal(
                    session,
                    db_user=fx.l2_a1_user,
                    is_reseller_bot=True,
                    reseller_profile_id=int(fx.pa.id),
                    reseller_owner_id=int(fx.ua.id),
                )
                self.assertIsNotNone(shop_as_l2_chatter)
                self.assertNotEqual(int(shop_as_l2_chatter.id), int(fx.a1.id))
                self.assertFalse(is_owner_principal(shop_as_l2_chatter))
                self.assertEqual(int(shop_as_l2_chatter.depth), 1)
                shop_gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_a1_user,
                    action="list",
                    callback_data="adm:pg:users",
                    is_reseller_bot=True,
                    reseller_profile_id=int(fx.pa.id),
                    reseller_owner_id=int(fx.ua.id),
                )
                self.assertFalse(shop_gate.allowed)

    async def test_af_ag_ah_pg_clients(self) -> None:
        from app.api.pg_pages import _staff_pg

        async with self.Session() as session:
            fx = await self._tree(session)
            fake_l2 = object()
            fake_l1 = object()
            live, feats = self._pg_live()
            with live, feats, self._admin_ids(OWNER_TID), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("Owner get_pg()"),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                side_effect=AssertionError("L1 reseller client"),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake_l2),
            ):
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_a1_user, **self._l2_kw(fx)
                )
                client, as_owner = await bot_pg_client_for_resolution(session, bridge)
                staff_a1 = await resolve_principal_web_session(
                    session,
                    build_principal_session_payload(
                        await authenticate_level1_web(
                            session, username="pg_a1", password=_WEB
                        )
                    ),
                )
                web_client, web_as_owner = await _staff_pg(session, staff_a1)
            self.assertIs(client, fake_l2)
            self.assertFalse(as_owner)
            self.assertIs(web_client, fake_l2)
            self.assertFalse(web_as_owner)
            with live, feats, self._admin_ids(OWNER_TID), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("L1 must not use Owner get_pg()"),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_l1),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                side_effect=AssertionError("L1 Bot uses get_pg_for_reseller"),
            ):
                l1_bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.ua, is_reseller_bot=False
                )
                l1_client, l1_as_owner = await bot_pg_client_for_resolution(
                    session, l1_bridge
                )
            self.assertIs(l1_client, fake_l1)
            self.assertFalse(l1_as_owner)

    async def test_ai_aj_web_bot_same_principal_and_scope(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            staff_a1 = await self._web(session, "pg_a1")
            staff_l1a = await self._web(session, "pg_shopa")
            live, feats = self._pg_live()
            with live, feats, self._admin_ids(OWNER_TID):
                bot_a1 = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_a1_user, **self._l2_kw(fx)
                )
                bot_l1 = await resolve_bot_principal_bridge(
                    session, db_user=fx.ua, is_reseller_bot=False
                )
            self.assertEqual(int(staff_a1["org_principal_id"]), int(bot_a1.principal.id))
            self.assertEqual(int(staff_a1["org_depth"]), int(bot_a1.authz.depth))
            self.assertEqual(
                frozenset(int(x) for x in staff_a1["org_visible_principal_ids"]),
                bot_a1.authz.visible_principal_ids,
            )
            self.assertEqual(int(staff_l1a["org_principal_id"]), int(bot_l1.principal.id))
            self.assertEqual(
                frozenset(int(x) for x in staff_l1a["org_visible_principal_ids"]),
                bot_l1.authz.visible_principal_ids,
            )
            ctx_a1 = authz_from_staff(staff_a1)
            self.assertFalse(is_explicit_org_owner(ctx_a1))
            self.assertFalse(has_org_global_scope(ctx_a1))
            denied = authorize(
                authenticated=True,
                ctx=ctx_a1,
                resource_principal_id=int(fx.l1a.id),
                pg_permission_ok=True,
                local_safety_ok=True,
            )
            self.assertFalse(denied.allowed)
            self.assertEqual(denied.reason, "resource_out_of_scope")
            own = authorize(
                authenticated=True,
                ctx=ctx_a1,
                resource_principal_id=int(fx.a1.id),
                pg_permission_ok=True,
                local_safety_ok=True,
            )
            self.assertTrue(own.allowed, own.reason)

    async def test_pg_role_name_never_hierarchy(self) -> None:
        async with self.Session() as session:
            fx = await self._tree(session)
            role = {
                "id": 99,
                "name": "Owner",
                "is_owner": True,
                "permissions": USERS_FULL,
            }
            live = patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=99),
            )
            feats = patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=(["pg_users", "pg_admins", "pg_overview"], role)),
            )
            with live, feats:
                staff = await resolve_principal_web_session(
                    session,
                    build_principal_session_payload(
                        await authenticate_level1_web(
                            session, username="pg_a1", password=_WEB
                        )
                    ),
                )
            self.assertFalse(staff.get("pg_is_owner"))
            self.assertFalse(staff.get("web_owner"))
            self.assertEqual(int(staff["org_depth"]), 2)
            self.assertNotIn("pg_admins", staff.get("pg_permissions") or [])
            self.assertFalse(is_explicit_owner_staff(staff))


if __name__ == "__main__":
    unittest.main()
