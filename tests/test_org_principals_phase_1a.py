"""Phase 1A — org_principals foundation (no scope / authz refactor)."""

from __future__ import annotations

import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401 — register metadata
from app.db.models import BotUser, OrgPrincipal, PgStaffAccess, ResellerProfile, Role
from app.services import org_principals as op
from tests.postgres_support import scoped_async_engine


class OrgPrincipalsPhase1ATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_ensure_owner_exactly_one(self) -> None:
        async with self.Session() as session:
            a = await op.ensure_owner_principal(session)
            b = await op.ensure_owner_principal(session)
            await session.commit()
            self.assertEqual(a.id, b.id)
            self.assertTrue(op.is_owner_principal(a))
            self.assertEqual(a.depth, 0)
            self.assertIsNone(a.parent_id)
            owners = list(
                (
                    await session.execute(
                        select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                    )
                )
                .scalars()
                .all()
            )
            active = [x for x in owners if x.status == op.STATUS_ACTIVE]
            self.assertEqual(len(active), 1)

    async def test_valid_depth_0_1_2(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            d1 = await op.create_principal(session, parent_id=owner.id, depth=1)
            d2 = await op.create_principal(session, parent_id=d1.id, depth=2)
            await session.commit()
            self.assertEqual(d1.depth, 1)
            self.assertEqual(d1.parent_id, owner.id)
            self.assertEqual(d2.depth, 2)
            self.assertEqual(d2.parent_id, d1.id)

    async def test_depth_gt_2_rejected(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            d1 = await op.create_principal(session, parent_id=owner.id, depth=1)
            d2 = await op.create_principal(session, parent_id=d1.id, depth=2)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=d2.id, depth=3)

    async def test_depth_2_cannot_have_owner_parent(self) -> None:
        async with self.Session() as session:
            owner = await op.ensure_owner_principal(session)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=owner.id, depth=2)

    async def test_invalid_parent_rejected(self) -> None:
        async with self.Session() as session:
            await op.ensure_owner_principal(session)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=None, depth=1)
            with self.assertRaises(op.OrgPrincipalError):
                await op.create_principal(session, parent_id=99999, depth=1)

    async def test_reseller_mapping_depth_1(self) -> None:
        async with self.Session() as session:
            user = BotUser(
                telegram_id=9001,
                role=Role.RESELLER.value,
                referral_code="r9001",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(user_id=user.id, is_active=True)
            session.add(profile)
            await session.flush()
            mapped = await op.map_reseller_profile_to_principal(session, profile)
            await session.commit()
            self.assertIsNotNone(mapped)
            assert mapped is not None
            self.assertEqual(mapped.depth, 1)
            self.assertEqual(mapped.reseller_profile_id, profile.id)
            self.assertEqual(mapped.bot_user_id, user.id)
            owner = await op.get_active_owner(session)
            assert owner is not None
            self.assertEqual(mapped.parent_id, owner.id)
            again = await op.map_reseller_profile_to_principal(session, profile)
            assert again is not None
            self.assertEqual(again.id, mapped.id)

    async def test_legacy_pg_staff_unassigned_without_parent(self) -> None:
        async with self.Session() as session:
            await op.ensure_owner_principal(session)
            staff = PgStaffAccess(
                pg_username="staff1",
                web_username="staff1",
                web_password_hash="x",
                is_active=True,
            )
            session.add(staff)
            await session.flush()
            resolved = await op.resolve_pg_staff_principal(session, staff)
            await session.commit()
            self.assertIsInstance(resolved, op.LegacyUnassigned)
            assert isinstance(resolved, op.LegacyUnassigned)
            self.assertEqual(resolved.pg_staff_id, staff.id)
            rows = list(
                (
                    await session.execute(
                        select(OrgPrincipal).where(OrgPrincipal.pg_staff_id == staff.id)
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(rows, [])

    async def test_role_admin_alone_does_not_create_owner(self) -> None:
        self.assertFalse(op.role_string_implies_owner("admin"))
        self.assertFalse(op.role_string_implies_owner(Role.ADMIN.value))
        async with self.Session() as session:
            session.add(
                BotUser(telegram_id=1, role=Role.ADMIN.value, referral_code="adm1")
            )
            await session.commit()
            self.assertIsNone(await op.get_active_owner(session))
            owner = await op.ensure_owner_principal(session)
            self.assertTrue(op.is_owner_principal(owner))


if __name__ == "__main__":
    unittest.main()
