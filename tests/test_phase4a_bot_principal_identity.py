"""Phase 4A — Bot Principal identity bridge tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
from app.services.authz import (
    authz_from_bot_staff,
    has_org_global_scope,
    is_explicit_org_owner,
)
from app.services.bot_principal_identity import (
    bot_pg_client_for_resolution,
    callback_carries_principal_tamper,
    resolve_bot_org_principal,
    resolve_bot_principal_bridge,
)
from app.services.org_principals import (
    bind_reseller_profile_principal,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff
from app.services.shop_scope import shop_owner_id
from tests.postgres_support import scoped_async_engine


class Phase4ABotPrincipalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _shop(
        self, session, *, tid: int, code: str, uname: str
    ) -> tuple[BotUser, ResellerProfile]:
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
        )
        session.add(profile)
        await session.flush()
        return user, profile

    async def test_1_web_owner_equals_bot_owner_principal(self) -> None:
        from app.bot.auth import resolve_bot_owner_principal

        async with self.Session() as session:
            web_owner = await ensure_owner_principal(session)
            bot_user = BotUser(
                telegram_id=88001,
                role=Role.ADMIN.value,
                referral_code="own4a",
            )
            session.add(bot_user)
            await session.commit()
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={88001}),
            ):
                bot_owner = await resolve_bot_owner_principal(
                    session, bot_user, is_reseller_bot=False
                )
            self.assertIsNotNone(bot_owner)
            assert bot_owner is not None
            self.assertEqual(int(bot_owner.id), int(web_owner.id))

    async def test_2_role_admin_without_admin_ids_denies_owner(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            admin_only = BotUser(
                telegram_id=88002,
                role=Role.ADMIN.value,
                referral_code="adm4a",
            )
            session.add(admin_only)
            await session.commit()
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids=frozenset()),
            ):
                got = await resolve_bot_org_principal(
                    session, db_user=admin_only, is_reseller_bot=False
                )
            self.assertIsNone(got)

    async def test_3_admin_ids_without_session_denies(self) -> None:
        user = BotUser(
            telegram_id=88003,
            role=Role.USER.value,
            referral_code="u4a3",
        )
        with patch(
            "app.config.get_settings",
            return_value=SimpleNamespace(admin_ids={88003}),
        ):
            got = await resolve_bot_org_principal(
                None, db_user=user, is_reseller_bot=False
            )
        self.assertIsNone(got)

    async def test_3b_admin_ids_with_disabled_owner_conflict_denies(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            conflict = OrgPrincipal(
                parent_id=None,
                depth=0,
                status="disabled",
            )
            session.add(conflict)
            with self.assertRaises(IntegrityError):
                await session.flush()
            await session.rollback()

            owner = (
                await session.execute(
                    select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                )
            ).scalar_one()
            owner.status = "disabled"
            user = BotUser(
                telegram_id=88004,
                role=Role.USER.value,
                referral_code="u4b",
            )
            session.add(user)
            await session.commit()
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={88004}),
            ):
                got = await resolve_bot_org_principal(
                    session, db_user=user, is_reseller_bot=False
                )
            self.assertIsNone(got)

    async def test_4_reseller_bot_resolves_own_l1(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            shop_user, profile = await self._shop(
                session, tid=88010, code="s4a", uname="shop4a"
            )
            customer = BotUser(
                telegram_id=88011,
                role=Role.USER.value,
                referral_code="cust4a",
                reseller_id=shop_user.id,
            )
            session.add(customer)
            await session.commit()
            got = await resolve_bot_org_principal(
                session,
                db_user=customer,
                is_reseller_bot=True,
                reseller_profile_id=int(profile.id),
                reseller_owner_id=int(shop_user.id),
            )
            self.assertIsNotNone(got)
            assert got is not None
            self.assertEqual(int(got.depth), 1)
            self.assertEqual(int(got.bot_user_id), int(shop_user.id))
            self.assertEqual(int(got.reseller_profile_id), int(profile.id))

    async def test_5_reseller_bot_cannot_resolve_owner(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            shop_user, profile = await self._shop(
                session, tid=88020, code="s5a", uname="shop5a"
            )
            await session.commit()
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={88020}),
            ):
                got = await resolve_bot_org_principal(
                    session,
                    db_user=shop_user,
                    is_reseller_bot=True,
                    reseller_profile_id=int(profile.id),
                    reseller_owner_id=int(shop_user.id),
                    spoof_org_principal_id=int(owner.id),
                )
            self.assertIsNotNone(got)
            assert got is not None
            self.assertFalse(is_owner_principal(got))
            self.assertNotEqual(int(got.id), int(owner.id))

    async def test_6_platform_reseller_resolves_own_principal(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            user, profile = await self._shop(
                session, tid=88030, code="s6a", uname="shop6a"
            )
            await bind_reseller_profile_principal(session, profile)
            await session.commit()
            got = await resolve_bot_org_principal(
                session, db_user=user, is_reseller_bot=False
            )
            self.assertIsNotNone(got)
            assert got is not None
            bound = await bind_reseller_profile_principal(session, profile)
            self.assertEqual(int(got.id), int(bound.id))

    async def test_7_reseller_a_cannot_resolve_b(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            ua, pa = await self._shop(session, tid=88041, code="a7", uname="shop_a7")
            ub, pb = await self._shop(session, tid=88042, code="b7", uname="shop_b7")
            pa_principal = await bind_reseller_profile_principal(session, pa)
            pb_principal = await bind_reseller_profile_principal(session, pb)
            await session.commit()
            got_a = await resolve_bot_org_principal(
                session, db_user=ua, is_reseller_bot=False
            )
            got_b = await resolve_bot_org_principal(
                session, db_user=ub, is_reseller_bot=False
            )
            self.assertEqual(int(got_a.id), int(pa_principal.id))
            self.assertEqual(int(got_b.id), int(pb_principal.id))
            self.assertNotEqual(int(got_a.id), int(got_b.id))
            vis_a = await visible_principal_ids(session, got_a)
            self.assertNotIn(int(got_b.id), vis_a)

    async def test_8_disabled_principal_denies(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            user, profile = await self._shop(
                session, tid=88050, code="s8", uname="shop8"
            )
            principal = await bind_reseller_profile_principal(session, profile)
            principal.status = "disabled"
            await session.commit()
            got = await resolve_bot_org_principal(
                session, db_user=user, is_reseller_bot=False
            )
            self.assertIsNone(got)

    async def test_9_callback_tamper_does_not_change_resolution(self) -> None:
        self.assertTrue(
            callback_carries_principal_tamper("adm:x:org_principal_id=1")
        )
        async with self.Session() as session:
            await ensure_owner_principal(session)
            ua, pa = await self._shop(session, tid=88061, code="a9", uname="shop_a9")
            ub, pb = await self._shop(session, tid=88062, code="b9", uname="shop_b9")
            pb_principal = await bind_reseller_profile_principal(session, pb)
            await session.commit()
            got = await resolve_bot_org_principal(
                session,
                db_user=ua,
                is_reseller_bot=False,
                spoof_org_principal_id=int(pb_principal.id),
            )
            self.assertIsNotNone(got)
            assert got is not None
            self.assertNotEqual(int(got.id), int(pb_principal.id))

    async def test_10_owner_pg_only_for_explicit_owner(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            owner_user = BotUser(
                telegram_id=88070,
                role=Role.USER.value,
                referral_code="ownpg",
            )
            shop_user, profile = await self._shop(
                session, tid=88071, code="s10", uname="shop10"
            )
            session.add(owner_user)
            await bind_reseller_profile_principal(session, profile)
            await session.commit()

            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={88070}),
            ):
                owner_bridge = await resolve_bot_principal_bridge(
                    session, db_user=owner_user, is_reseller_bot=False
                )
            assert owner_bridge is not None
            fake_owner_pg = object()
            with patch(
                "app.services.pasarguard.get_pg",
                return_value=fake_owner_pg,
            ):
                oc, o_as_owner = await bot_pg_client_for_resolution(
                    session, owner_bridge
                )
            self.assertIs(oc, fake_owner_pg)

            l1_bridge = await resolve_bot_principal_bridge(
                session, db_user=shop_user, is_reseller_bot=False
            )
            assert l1_bridge is not None
            fake_shop_pg = MagicMock()
            with patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=fake_shop_pg),
            ) as gr:
                with patch(
                    "app.services.pasarguard.get_pg",
                    return_value=MagicMock(),
                ) as gp:
                    sc, s_as_owner = await bot_pg_client_for_resolution(
                        session, l1_bridge
                    )
            self.assertIs(sc, fake_shop_pg)
            self.assertFalse(s_as_owner)
            gp.assert_not_called()
            gr.assert_awaited_once()

    async def test_bridge_authz_matches_web_fields(self) -> None:
        async with self.Session() as session:
            web_owner = await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=88080,
                role=Role.ADMIN.value,
                referral_code="br80",
            )
            session.add(user)
            await session.commit()
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids={88080}),
            ), patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(
                    return_value={
                        "ok": True,
                        "features": ["pg_users"],
                        "pg_is_owner": True,
                        "username": "env_owner",
                    }
                ),
            ):
                bridge = await resolve_bot_principal_bridge(
                    session, db_user=user, is_reseller_bot=False
                )
            assert bridge is not None
            self.assertEqual(int(bridge.principal.id), int(web_owner.id))
            ctx = authz_from_bot_staff(bridge.staff)
            self.assertTrue(is_explicit_org_owner(ctx))
            self.assertTrue(has_org_global_scope(ctx))
            self.assertTrue(is_explicit_owner_staff(bridge.staff))

    async def test_l1_shop_scope_still_bot_user_id(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            shop_user, profile = await self._shop(
                session, tid=88090, code="s90", uname="shop90"
            )
            await bind_reseller_profile_principal(session, profile)
            await session.commit()
            bridge = await resolve_bot_principal_bridge(
                session,
                db_user=shop_user,
                is_reseller_bot=True,
                reseller_profile_id=int(profile.id),
                reseller_owner_id=int(shop_user.id),
            )
            assert bridge is not None
            self.assertEqual(shop_owner_id(bridge.staff), int(shop_user.id))
            self.assertFalse(is_explicit_owner_staff(bridge.staff))

    async def test_missing_profile_denies(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            await session.commit()
            got = await resolve_bot_org_principal(
                session,
                db_user=None,
                is_reseller_bot=True,
                reseller_profile_id=99999,
                reseller_owner_id=1,
            )
            self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
