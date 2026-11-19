"""Phase 3F.1 — ResellerProfile → depth-1 Principal adapter wiring.

Cookie hierarchy fields are never selectors. Server-loaded ResellerProfile is.
"""

from __future__ import annotations

import pathlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, PgStaffAccess, ResellerProfile, Role
from app.services.org_principals import (
    attach_org_principal_fields,
    bind_reseller_profile_principal,
    ensure_owner_principal,
    is_owner_principal,
    map_reseller_profile_to_principal,
    resolve_org_principal_for_staff,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff
from app.services.resellers import make_reseller
from app.services.shop_scope import (
    assert_order_in_scope,
    is_platform_admin,
    shop_owner_id,
)
from tests.postgres_support import scoped_async_engine


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_PY = (ROOT / "app" / "api" / "app.py").read_text(encoding="utf-8")
_STAFF_FN = APP_PY[
    APP_PY.find("async def require_staff") : APP_PY.find("async def require_admin")
]


class Phase3F1ResellerAdapterTests(unittest.IsolatedAsyncioTestCase):
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
        self, session, *, telegram_id: int, code: str, username: str
    ) -> tuple[BotUser, ResellerProfile]:
        user = BotUser(
            telegram_id=telegram_id,
            role=Role.RESELLER.value,
            referral_code=code,
        )
        session.add(user)
        await session.flush()
        profile = ResellerProfile(
            user_id=user.id,
            is_active=True,
            web_username=username,
            web_password_hash="x" * 24,
            setup_completed_at=datetime.now(timezone.utc),
            pg_admin_username=f"pg_{username}",
        )
        session.add(profile)
        await session.flush()
        return user, profile

    def _staff_from(self, profile: ResellerProfile, principal, visible) -> dict:
        return attach_org_principal_fields(
            {
                "role": "reseller",
                "bot_user_id": int(profile.user_id),
                "pg_admin_username": profile.pg_admin_username,
            },
            principal,
            visible_principal_ids=visible,
        )

    def test_require_staff_wires_adapter_not_cookie_principal_id(self) -> None:
        self.assertIn("bind_reseller_profile_principal", _STAFF_FN)
        self.assertIn("user.pop(\"org_principal_id\", None)", _STAFF_FN)
        self.assertIn("user.pop(\"org_parent_id\", None)", _STAFF_FN)
        self.assertIn("user.pop(\"org_depth\", None)", _STAFF_FN)
        self.assertIn("user[\"reseller_profile_id\"] = int(profile.id)", _STAFF_FN)
        self.assertIn("if principal is None:", _STAFF_FN)
        # Cookie dict is not the reseller Principal selector.
        reseller_chunk = _STAFF_FN[
            _STAFF_FN.find('if user.get("role") == "reseller"') : _STAFF_FN.find(
                'elif user.get("role") == "pg_staff"'
            )
        ]
        self.assertNotIn("resolve_org_principal_for_staff", reseller_chunk)

    async def test_a_valid_reseller_own_depth1(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            _u, profile = await self._shop(
                session, telegram_id=6101, code="ra1", username="shop_a"
            )
            principal = await bind_reseller_profile_principal(session, profile)
            await session.commit()
            self.assertIsNotNone(principal)
            assert principal is not None
            self.assertEqual(int(principal.depth), 1)
            self.assertEqual(int(principal.reseller_profile_id), int(profile.id))
            self.assertEqual(int(principal.bot_user_id), int(profile.user_id))
            self.assertEqual(int(principal.parent_id), int(owner.id))
            self.assertFalse(is_owner_principal(principal))
            vis = await visible_principal_ids(session, principal)
            self.assertIn(int(principal.id), vis)
            self.assertNotIn(int(owner.id), vis)

    async def test_a_idempotent_no_duplicate_rows(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            _u, profile = await self._shop(
                session, telegram_id=6102, code="ra2", username="shop_a2"
            )
            p1 = await bind_reseller_profile_principal(session, profile)
            p2 = await bind_reseller_profile_principal(session, profile)
            p3 = await map_reseller_profile_to_principal(session, profile)
            await session.commit()
            self.assertEqual(int(p1.id), int(p2.id))
            self.assertEqual(int(p1.id), int(p3.id))
            rows = (
                await session.execute(
                    select(OrgPrincipal).where(
                        OrgPrincipal.reseller_profile_id == int(profile.id)
                    )
                )
            ).scalars().all()
            self.assertEqual(len(rows), 1)

    async def test_b_reseller_a_cannot_resolve_principal_b(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            _ua, pa = await self._shop(
                session, telegram_id=6201, code="rb1", username="shop_aa"
            )
            _ub, pb = await self._shop(
                session, telegram_id=6202, code="rb2", username="shop_bb"
            )
            a = await bind_reseller_profile_principal(session, pa)
            b = await bind_reseller_profile_principal(session, pb)
            await session.commit()
            self.assertNotEqual(int(a.id), int(b.id))
            vis_a = await visible_principal_ids(session, a)
            vis_b = await visible_principal_ids(session, b)
            self.assertNotIn(int(b.id), vis_a)
            self.assertNotIn(int(a.id), vis_b)
            # Spoofed cookie principal id of B with A's profile still binds A.
            spoof = {
                "role": "reseller",
                "bot_user_id": int(pa.user_id),
                "org_principal_id": int(b.id),
                "org_depth": 0,
            }
            bound = await bind_reseller_profile_principal(session, pa)
            self.assertEqual(int(bound.id), int(a.id))
            resolved = await resolve_org_principal_for_staff(session, spoof)
            if resolved is not None:
                self.assertNotEqual(int(resolved.id), int(b.id))

    async def test_c_reseller_cannot_resolve_owner(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            _u, profile = await self._shop(
                session, telegram_id=6301, code="rc1", username="shop_c"
            )
            principal = await bind_reseller_profile_principal(session, profile)
            await session.commit()
            self.assertFalse(is_owner_principal(principal))
            self.assertNotEqual(int(principal.id), int(owner.id))
            spoof = {
                "role": "reseller",
                "bot_user_id": int(profile.user_id),
                "org_principal_id": int(owner.id),
                "web_owner": True,
                "role_admin": "admin",
            }
            resolved = await resolve_org_principal_for_staff(session, spoof)
            if resolved is not None:
                self.assertFalse(is_owner_principal(resolved))
            staff = self._staff_from(
                profile, principal, await visible_principal_ids(session, principal)
            )
            self.assertFalse(is_explicit_owner_staff(staff))
            self.assertFalse(is_platform_admin(staff))

    async def test_d_missing_profile_denies(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            await session.commit()
            self.assertIsNone(await bind_reseller_profile_principal(session, None))
            self.assertIsNone(
                await resolve_org_principal_for_staff(
                    session, {"role": "reseller", "bot_user_id": 555}
                )
            )
            ghost = ResellerProfile(user_id=0, is_active=True)
            self.assertIsNone(await bind_reseller_profile_principal(session, ghost))

    async def test_d_inactive_profile_denies(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=6401, role=Role.RESELLER.value, referral_code="rd1"
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(user_id=user.id, is_active=False)
            session.add(profile)
            await session.flush()
            self.assertIsNone(await bind_reseller_profile_principal(session, profile))

    async def test_e_f_no_owner_pg_uses_reseller_client(self) -> None:
        from app.api.pg_pages import _staff_pg

        async with self.Session() as session:
            await ensure_owner_principal(session)
            _u, profile = await self._shop(
                session, telegram_id=6501, code="re1", username="shop_e"
            )
            principal = await bind_reseller_profile_principal(session, profile)
            staff = self._staff_from(
                profile, principal, await visible_principal_ids(session, principal)
            )
            fake = MagicMock()
            with (
                patch("app.api.pg_pages.get_pg") as gp,
                patch(
                    "app.api.pg_pages.get_pg_for_reseller",
                    new=AsyncMock(return_value=fake),
                ) as gr,
            ):
                client, as_owner = await _staff_pg(session, staff)
            self.assertIs(client, fake)
            self.assertFalse(as_owner)
            gp.assert_not_called()
            gr.assert_awaited()
            self.assertEqual(int(gr.await_args.args[1]), int(profile.user_id))

    async def test_g_shop_scope_still_reseller_id(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            user, profile = await self._shop(
                session, telegram_id=6601, code="rg1", username="shop_g"
            )
            principal = await bind_reseller_profile_principal(session, profile)
            staff = self._staff_from(
                profile, principal, await visible_principal_ids(session, principal)
            )
            self.assertEqual(shop_owner_id(staff), int(user.id))
            mine = SimpleNamespace(reseller_id=int(user.id))
            other = SimpleNamespace(reseller_id=int(user.id) + 99)
            assert_order_in_scope(staff, mine)
            from app.services.shop_scope import ShopScopeError

            with self.assertRaises(ShopScopeError):
                assert_order_in_scope(staff, other)

    async def test_h_unmapped_pg_staff_unchanged(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            row = PgStaffAccess(
                pg_username="staff_h",
                web_username="staff_h",
                web_password_hash="h",
                is_active=True,
            )
            session.add(row)
            await session.flush()
            got = await resolve_org_principal_for_staff(
                session, {"role": "pg_staff", "pg_staff_id": int(row.id)}
            )
            self.assertIsNone(got)

    async def test_i_role_admin_not_used_for_reseller_owner(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            _u, profile = await self._shop(
                session, telegram_id=6701, code="ri1", username="shop_i"
            )
            principal = await bind_reseller_profile_principal(session, profile)
            staff = self._staff_from(
                profile, principal, await visible_principal_ids(session, principal)
            )
            staff["role"] = "admin"
            # Even if role string is swapped, bind came from profile — not Owner.
            self.assertNotEqual(int(staff["org_principal_id"]), int(owner.id))
            self.assertEqual(int(staff["org_depth"]), 1)
            self.assertFalse(is_owner_principal(principal))

    async def test_fresh_make_reseller_maps_principal(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            user = BotUser(
                telegram_id=6801, role=Role.USER.value, referral_code="rf1"
            )
            session.add(user)
            await session.flush()
            profile = await make_reseller(
                session,
                user,
                web_username="fresh_shop",
                web_password_hash="y" * 24,
            )
            principal = await bind_reseller_profile_principal(session, profile)
            self.assertIsNotNone(principal)
            assert principal is not None
            self.assertEqual(int(principal.depth), 1)
            self.assertEqual(int(principal.parent_id), int(owner.id))
            self.assertEqual(int(principal.reseller_profile_id), int(profile.id))
            again = await bind_reseller_profile_principal(session, profile)
            self.assertEqual(int(again.id), int(principal.id))


if __name__ == "__main__":
    unittest.main()
