"""Phase 5B — L2 Bot Telegram bind + trusted resolution."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
from app.services.authz import (
    authz_from_staff,
    has_active_org_principal,
    has_org_global_scope,
    is_explicit_org_owner,
)
from app.services.bot_l2_bind import L2BotBindError, bind_l2_bot_telegram
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
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_web_identity import attach_level2_web_identity
from app.services.shop_scope import shop_owner_id
from tests.postgres_support import scoped_async_engine


def _owner_staff(owner: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {"role": "admin", "web_owner": True},
        owner,
    )


def _l1_staff(principal: OrgPrincipal, user: BotUser, profile: ResellerProfile) -> dict:
    return attach_org_principal_fields(
        {
            "role": "reseller",
            "bot_user_id": int(user.id),
            "reseller_profile_id": int(profile.id),
        },
        principal,
    )


class Phase5BBotL2BindResolveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
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
        user = await self._user(
            session, tid=tid, code=code, role=Role.RESELLER.value
        )
        profile = ResellerProfile(
            user_id=user.id,
            is_active=True,
            web_username=uname,
            web_password_hash="x" * 24,
            setup_completed_at=datetime.now(timezone.utc),
            pg_admin_username=f"pg_{uname}",
            pg_admin_password_enc="enc",
        )
        session.add(profile)
        await session.flush()
        return user, profile

    async def _fixtures(self, session):
        owner_p = await ensure_owner_principal(session)
        owner_user = await self._user(
            session, tid=99001, code="own5b", role=Role.ADMIN.value
        )
        sticky = await self._user(
            session, tid=99002, code="stk5b", role=Role.ADMIN.value
        )
        ua, pa = await self._shop(session, tid=99010, code="a5b", uname="shopa")
        ub, pb = await self._shop(session, tid=99020, code="b5b", uname="shopb")
        pa_p = await bind_reseller_profile_principal(session, pa)
        pb_p = await bind_reseller_profile_principal(session, pb)
        a1 = await create_principal(
            session, parent_id=int(pa_p.id), depth=2, pg_username="pg_a1"
        )
        a2 = await create_principal(
            session, parent_id=int(pa_p.id), depth=2, pg_username="pg_a2"
        )
        b1 = await create_principal(
            session, parent_id=int(pb_p.id), depth=2, pg_username="pg_b1"
        )
        l2_user = await self._user(session, tid=99101, code="l2a1")
        sib_user = await self._user(session, tid=99102, code="l2a2")
        foreign_user = await self._user(session, tid=99121, code="l2b1")
        unbound = await self._user(session, tid=99199, code="unb5b")
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
            a1=a1,
            a2=a2,
            b1=b1,
            l2_user=l2_user,
            sib_user=sib_user,
            foreign_user=foreign_user,
            unbound=unbound,
        )

    def _admin_ids(self, *ids: int):
        return patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(admin_ids=set(ids)),
        )

    def _pg_quiet(self):
        return patch(
            "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
            new=AsyncMock(return_value=None),
        )

    async def _bind_own_child(self, session, fx, *, with_shop: bool = True) -> None:
        await bind_l2_bot_telegram(
            session,
            staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
            target_principal_id=int(fx.a1.id),
            telegram_id=int(fx.l2_user.telegram_id),
            admin_ids={99001},
        )
        if not with_shop:
            await session.commit()
            return
        shop = ResellerProfile(
            user_id=int(fx.l2_user.id),
            is_active=True,
            web_username="l2a1shop",
            web_password_hash="x" * 24,
            setup_completed_at=datetime.now(timezone.utc),
            pg_admin_username="pg_a1",
            pg_role_id=10,
        )
        session.add(shop)
        await session.flush()
        fx.a1.reseller_profile_id = int(shop.id)
        fx.a1.bot_user_id = int(fx.l2_user.id)
        fx.l2_shop = shop
        await session.commit()

    def _l2_kw(self, fx) -> dict:
        return {
            "is_reseller_bot": True,
            "reseller_profile_id": int(fx.l2_shop.id),
            "reseller_owner_id": int(fx.l2_user.id),
        }

    async def test_1_l2_resolves_own_principal(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            with self._admin_ids(99001), self._pg_quiet():
                owner_got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, is_reseller_bot=False
                )
                got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            self.assertIsNone(owner_got)
            self.assertIsNotNone(got)
            assert got is not None and bridge is not None
            self.assertEqual(int(got.id), int(fx.a1.id))
            self.assertEqual(int(got.depth), 2)
            self.assertEqual(bridge.channel, "principal_l2")
            self.assertFalse(is_owner_principal(got))
            ctx = bridge.authz
            self.assertEqual(int(ctx.principal_id or 0), int(fx.a1.id))
            self.assertEqual(ctx.depth, 2)
            self.assertEqual(int(ctx.parent_id or 0), int(fx.pa_p.id))
            self.assertTrue(has_active_org_principal(ctx))
            self.assertEqual(ctx.visible_principal_ids, frozenset({int(fx.a1.id)}))
            self.assertFalse(bool(bridge.staff.get("web_owner")))
            self.assertEqual(bridge.staff.get("pg_admin_username"), "pg_a1")
            self.assertEqual(int(shop_owner_id(bridge.staff) or 0), int(fx.l2_user.id))

    async def test_2_l2_cannot_resolve_sibling(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            await bind_l2_bot_telegram(
                session,
                staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                target_principal_id=int(fx.a2.id),
                telegram_id=int(fx.sib_user.telegram_id),
                admin_ids={99001},
            )
            await session.commit()
            with self._admin_ids(99001), self._pg_quiet():
                got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            assert got is not None
            self.assertEqual(int(got.id), int(fx.a1.id))
            self.assertNotEqual(int(got.id), int(fx.a2.id))
            visible = await visible_principal_ids(session, got)
            self.assertNotIn(int(fx.a2.id), visible)

    async def test_3_l2_cannot_resolve_parent(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            with self._admin_ids(99001), self._pg_quiet():
                got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            assert got is not None
            self.assertNotEqual(int(got.id), int(fx.pa_p.id))
            visible = await visible_principal_ids(session, got)
            self.assertNotIn(int(fx.pa_p.id), visible)

    async def test_4_l2_cannot_resolve_owner(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            with self._admin_ids(99001), self._pg_quiet():
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            assert bridge is not None
            self.assertFalse(is_owner_principal(bridge.principal))
            self.assertFalse(is_explicit_org_owner(bridge.authz))
            self.assertFalse(has_org_global_scope(bridge.authz))
            self.assertFalse(is_explicit_owner_staff(bridge.staff))
            self.assertNotEqual(int(bridge.principal.id), int(fx.owner_p.id))

    async def test_5_disabled_l2_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            fx.a1.status = "disabled"
            await session.commit()
            with self._admin_ids(99001):
                got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            self.assertIsNone(got)

    async def test_6_disabled_l1_parent_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            fx.pa_p.status = "disabled"
            await session.commit()
            with self._admin_ids(99001):
                got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            self.assertIsNone(got)

    async def test_7_unbound_telegram_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self._admin_ids(99001):
                got = await resolve_bot_org_principal(
                    session, db_user=fx.unbound, is_reseller_bot=False
                )
            self.assertIsNone(got)

    async def test_8_admin_ids_collision_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self.assertRaises(L2BotBindError) as ctx:
                await bind_l2_bot_telegram(
                    session,
                    staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                    target_principal_id=int(fx.a1.id),
                    telegram_id=int(fx.owner_user.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx.exception.code, "admin_ids_collision")
            fx.a1.bot_user_id = int(fx.owner_user.id)
            await session.commit()
            with self._admin_ids(99001), self._pg_quiet():
                got = await resolve_bot_org_principal(
                    session, db_user=fx.owner_user, is_reseller_bot=False
                )
            self.assertIsNotNone(got)
            assert got is not None
            self.assertTrue(is_owner_principal(got))
            self.assertNotEqual(int(got.depth), 2)

    async def test_9_reseller_profile_collision_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self.assertRaises(L2BotBindError) as ctx:
                await bind_l2_bot_telegram(
                    session,
                    staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                    target_principal_id=int(fx.a1.id),
                    telegram_id=int(fx.ua.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx.exception.code, "reseller_collision")

    async def test_10_existing_principal_binding_collision_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx, with_shop=False)
            with self.assertRaises(L2BotBindError) as ctx:
                await bind_l2_bot_telegram(
                    session,
                    staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                    target_principal_id=int(fx.a2.id),
                    telegram_id=int(fx.l2_user.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx.exception.code, "binding_collision")
            with self.assertRaises(L2BotBindError) as ctx2:
                await bind_l2_bot_telegram(
                    session,
                    staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                    target_principal_id=int(fx.a1.id),
                    telegram_id=int(fx.sib_user.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx2.exception.code, "already_bound")

    async def test_11_duplicate_bot_user_id_deny(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.a1.bot_user_id = int(fx.l2_user.id)
            fx.a2.bot_user_id = int(fx.l2_user.id)
            await session.commit()
            with self._admin_ids(99001):
                got = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, is_reseller_bot=False
                )
            self.assertIsNone(got)
            with self.assertRaises(L2BotBindError) as ctx:
                await bind_l2_bot_telegram(
                    session,
                    staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                    target_principal_id=int(fx.a1.id),
                    telegram_id=int(fx.l2_user.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx.exception.code, "duplicate_bot_user_id")

    async def test_12_callback_injection_ignored(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            self.assertTrue(
                callback_carries_principal_tamper(
                    f"adm:pg:users:org_principal_id:{fx.owner_p.id}:org_depth:0"
                )
            )
            with self._admin_ids(99001), self._pg_quiet():
                owner_got = await resolve_bot_org_principal(
                    session,
                    db_user=fx.l2_user,
                    is_reseller_bot=False,
                    spoof_org_principal_id=int(fx.owner_p.id),
                )
                shop_got = await resolve_bot_org_principal(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    spoof_org_principal_id=int(fx.owner_p.id),
                )
            self.assertIsNone(owner_got)
            assert shop_got is not None
            self.assertEqual(int(shop_got.id), int(fx.a1.id))

    async def test_13_role_admin_does_not_create_authority(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            fx.l2_user.role = Role.ADMIN.value
            await session.commit()
            await self._bind_own_child(session, fx)
            with self._admin_ids(99001), self._pg_quiet():
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
                sticky = await resolve_bot_org_principal(
                    session, db_user=fx.sticky, is_reseller_bot=False
                )
            assert bridge is not None
            self.assertEqual(int(bridge.principal.id), int(fx.a1.id))
            self.assertEqual(int(bridge.principal.depth), 2)
            self.assertFalse(is_explicit_org_owner(bridge.authz))
            self.assertIsNone(sticky)

    async def test_14_shop_bot_never_resolves_l2(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            with self._admin_ids(99001), self._pg_quiet():
                shop_l2 = await resolve_bot_org_principal(
                    session,
                    db_user=fx.l2_user,
                    is_reseller_bot=True,
                    reseller_profile_id=int(fx.pa.id),
                    reseller_owner_id=int(fx.ua.id),
                )
                shop_no_token = await resolve_bot_org_principal(
                    session,
                    db_user=fx.l2_user,
                    is_reseller_bot=True,
                )
                shop_l1 = await resolve_bot_org_principal(
                    session,
                    db_user=fx.ua,
                    is_reseller_bot=True,
                    reseller_profile_id=int(fx.pa.id),
                    reseller_owner_id=int(fx.ua.id),
                )
            self.assertIsNotNone(shop_l2)
            assert shop_l2 is not None
            self.assertEqual(int(shop_l2.depth), 1)
            self.assertEqual(int(shop_l2.id), int(fx.pa_p.id))
            self.assertNotEqual(int(shop_l2.id), int(fx.a1.id))
            self.assertIsNone(shop_no_token)
            assert shop_l1 is not None
            self.assertEqual(int(shop_l1.depth), 1)

    async def test_15_l1_can_bind_only_direct_child(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            result = await bind_l2_bot_telegram(
                session,
                staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                target_principal_id=int(fx.a1.id),
                telegram_id=int(fx.l2_user.telegram_id),
                admin_ids={99001},
                bot_user_id=999999,
                parent_id=1,
                depth=0,
                pg_username="owner_pg",
                web_owner=True,
                role="admin",
            )
            self.assertTrue(result.created_binding)
            self.assertEqual(int(fx.a1.bot_user_id or 0), int(fx.l2_user.id))
            again = await bind_l2_bot_telegram(
                session,
                staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                target_principal_id=int(fx.a1.id),
                telegram_id=int(fx.l2_user.telegram_id),
                admin_ids={99001},
            )
            self.assertFalse(again.created_binding)

    async def test_16_l1_cannot_bind_sibling_or_foreign_l2(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            with self.assertRaises(L2BotBindError) as ctx:
                await bind_l2_bot_telegram(
                    session,
                    staff=_l1_staff(fx.pa_p, fx.ua, fx.pa),
                    target_principal_id=int(fx.b1.id),
                    telegram_id=int(fx.foreign_user.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx.exception.code, "not_direct_child")

    async def test_17_owner_can_bind_existing_l2(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            result = await bind_l2_bot_telegram(
                session,
                staff=_owner_staff(fx.owner_p),
                target_principal_id=int(fx.b1.id),
                telegram_id=int(fx.foreign_user.telegram_id),
                admin_ids={99001},
            )
            self.assertTrue(result.created_binding)
            self.assertEqual(int(fx.b1.bot_user_id or 0), int(fx.foreign_user.id))

    async def test_18_l2_cannot_perform_bind(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            ident = await attach_level2_web_identity(
                session,
                principal_id=int(fx.a1.id),
                web_username="pg_a1",
                password="Aa1aaaaa",
            )
            await session.commit()
            l2_staff = {"role": "principal", "web_identity_id": int(ident.id)}
            with self.assertRaises(L2BotBindError) as ctx:
                await bind_l2_bot_telegram(
                    session,
                    staff=l2_staff,
                    target_principal_id=int(fx.a2.id),
                    telegram_id=int(fx.sib_user.telegram_id),
                    admin_ids={99001},
                )
            self.assertEqual(ctx.exception.code, "not_authorized")

    async def test_19_web_l2_principal_equals_bot_l2(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            ident = await attach_level2_web_identity(
                session,
                principal_id=int(fx.a1.id),
                web_username="pg_a1",
                password="Aa1aaaaa",
            )
            await self._bind_own_child(session, fx)
            from app.services.org_principals import resolve_org_principal_for_staff

            web = await resolve_org_principal_for_staff(
                session, {"role": "principal", "web_identity_id": int(ident.id)}
            )
            with self._admin_ids(99001), self._pg_quiet():
                bot = await resolve_bot_org_principal(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            self.assertIsNotNone(web)
            self.assertIsNotNone(bot)
            assert web is not None and bot is not None
            self.assertEqual(int(web.id), int(bot.id))
            self.assertEqual(int(web.id), int(fx.a1.id))
            web_ctx = authz_from_staff(
                attach_org_principal_fields(
                    {"role": "principal", "web_owner": False},
                    web,
                    visible_principal_ids=await visible_principal_ids(session, web),
                )
            )
            with self._pg_quiet():
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
            assert bridge is not None
            self.assertEqual(web_ctx.principal_id, bridge.authz.principal_id)
            self.assertEqual(web_ctx.depth, bridge.authz.depth)
            self.assertEqual(web_ctx.visible_principal_ids, bridge.authz.visible_principal_ids)

    async def test_20_l2_client_not_owner_or_parent(self) -> None:
        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            fake = object()
            with self._admin_ids(99001), self._pg_quiet(), patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("L2 must not use Owner get_pg()"),
            ), patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake),
            ), patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=fake),
            ):
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=fx.l2_user, **self._l2_kw(fx)
                )
                assert bridge is not None
                client, as_owner = await bot_pg_client_for_resolution(session, bridge)
            self.assertIs(client, fake)
            self.assertFalse(as_owner)

    async def test_21_l2_not_exposed_to_migrated_pg_bot_handlers(self) -> None:
        from app.bot.auth import bot_migrated_pg_features, bot_pg_can_create_user
        from app.services.bot_pg_user_pilot import authorize_bot_pg_user_op

        async with self.Session() as session:
            fx = await self._fixtures(session)
            await self._bind_own_child(session, fx)
            with self._admin_ids(99001), self._pg_quiet():
                feats = await bot_migrated_pg_features(
                    session, fx.l2_user, **self._l2_kw(fx)
                )
                can_create = await bot_pg_can_create_user(
                    session, fx.l2_user, **self._l2_kw(fx)
                )
                gate = await authorize_bot_pg_user_op(
                    session,
                    db_user=fx.l2_user,
                    **self._l2_kw(fx),
                    action="list",
                    callback_data="adm:pg:users",
                )
            self.assertEqual(feats, frozenset())
            self.assertFalse(can_create)
            self.assertFalse(gate.allowed)


if __name__ == "__main__":
    unittest.main()
