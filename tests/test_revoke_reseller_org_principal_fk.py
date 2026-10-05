"""Regression: revoke/delete reseller must not trip org_principals FK.

Root cause: revoke_reseller deleted ResellerProfile while OrgPrincipal still
held reseller_profile_id → IntegrityError (org_principals_reseller_profile_id_fkey).
Same class of bug for PgStaffAccess ↔ org_principals / panel_tickets.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _enable_sqlite_fk(dbapi_conn, _connection_record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


class RevokeResellerOrgPrincipalFkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "revoke_fk.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        event.listen(self.engine.sync_engine, "connect", _enable_sqlite_fk)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                __import__("sqlalchemy").text("PRAGMA foreign_keys=ON")
            )
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _seed_owner(self, session):
        from app.services.org_principals import ensure_owner_principal

        return await ensure_owner_principal(session)

    async def test_revoke_reseller_with_org_principal_succeeds(self):
        from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
        from app.services.org_principals import (
            DEPTH_ONE,
            STATUS_ACTIVE,
            create_principal,
            get_principal_by_reseller_profile,
        )
        from app.services.resellers import revoke_reseller

        async with self.Session() as session:
            owner = await self._seed_owner(session)
            user = BotUser(
                telegram_id=950001,
                username="rs_fk",
                full_name="نماینده FK",
                role=Role.RESELLER.value,
                referral_code="RS950001",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="rs_fk_pg",
                is_active=True,
            )
            session.add(profile)
            await session.flush()
            principal = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=DEPTH_ONE,
                pg_username="rs_fk_pg",
                reseller_profile_id=int(profile.id),
                bot_user_id=int(user.id),
                status=STATUS_ACTIVE,
            )
            await session.commit()
            uid = int(user.id)
            pid = int(principal.id)
            profile_id = int(profile.id)

            mock_pg = AsyncMock()
            mock_pg.delete_admin = AsyncMock(return_value=None)
            with patch("app.services.pasarguard.get_pg", return_value=mock_pg):
                info = await revoke_reseller(session, uid, delete_pg_admin=True)

            self.assertEqual(info["user_id"], uid)
            self.assertIsNone(await session.get(ResellerProfile, profile_id))
            self.assertIsNone(await session.get(OrgPrincipal, pid))
            self.assertIsNone(
                await get_principal_by_reseller_profile(session, profile_id)
            )
            refreshed = await session.get(BotUser, uid)
            self.assertEqual(refreshed.role, Role.USER.value)

    async def test_revoke_reseller_purges_l2_child_and_child_shop(self):
        from app.db.models import BotUser, OrgPrincipal, ResellerProfile, Role
        from app.services.org_principals import (
            DEPTH_ONE,
            DEPTH_TWO,
            STATUS_ACTIVE,
            create_principal,
        )
        from app.services.resellers import revoke_reseller

        async with self.Session() as session:
            owner = await self._seed_owner(session)
            parent_user = BotUser(
                telegram_id=950010,
                username="rs_l1",
                full_name="L1",
                role=Role.RESELLER.value,
                referral_code="RS950010",
            )
            child_user = BotUser(
                telegram_id=950011,
                username="rs_l2",
                full_name="L2",
                role=Role.RESELLER.value,
                referral_code="RS950011",
            )
            session.add_all([parent_user, child_user])
            await session.flush()
            parent_profile = ResellerProfile(
                user_id=int(parent_user.id),
                pg_admin_username="rs_l1_pg",
                is_active=True,
            )
            child_profile = ResellerProfile(
                user_id=int(child_user.id),
                pg_admin_username="rs_l2_pg",
                is_active=True,
            )
            session.add_all([parent_profile, child_profile])
            await session.flush()
            l1 = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=DEPTH_ONE,
                pg_username="rs_l1_pg",
                reseller_profile_id=int(parent_profile.id),
                bot_user_id=int(parent_user.id),
                status=STATUS_ACTIVE,
            )
            l2 = await create_principal(
                session,
                parent_id=int(l1.id),
                depth=DEPTH_TWO,
                pg_username="rs_l2_pg",
                reseller_profile_id=int(child_profile.id),
                bot_user_id=int(child_user.id),
                status=STATUS_ACTIVE,
            )
            await session.commit()
            parent_uid = int(parent_user.id)
            child_uid = int(child_user.id)
            parent_pid = int(parent_profile.id)
            child_pid = int(child_profile.id)
            l1_id = int(l1.id)
            l2_id = int(l2.id)

            mock_pg = AsyncMock()
            mock_pg.delete_admin = AsyncMock(return_value=None)
            with patch("app.services.pasarguard.get_pg", return_value=mock_pg):
                await revoke_reseller(session, parent_uid, delete_pg_admin=True)

            self.assertIsNone(await session.get(ResellerProfile, parent_pid))
            self.assertIsNone(await session.get(ResellerProfile, child_pid))
            self.assertIsNone(await session.get(OrgPrincipal, l1_id))
            self.assertIsNone(await session.get(OrgPrincipal, l2_id))
            self.assertEqual(
                (await session.get(BotUser, parent_uid)).role, Role.USER.value
            )
            self.assertEqual(
                (await session.get(BotUser, child_uid)).role, Role.USER.value
            )

    async def test_revoke_clears_owner_principal_id_and_web_identity(self):
        from app.db.models import (
            BotUser,
            OrgPrincipal,
            OrgPrincipalWebIdentity,
            ResellerProfile,
            Role,
        )
        from app.services.org_principals import (
            DEPTH_ONE,
            STATUS_ACTIVE,
            create_principal,
        )
        from app.services.resellers import revoke_reseller
        from app.services.web_auth import hash_password

        async with self.Session() as session:
            owner = await self._seed_owner(session)
            user = BotUser(
                telegram_id=950020,
                username="rs_own",
                full_name="OwnerRef",
                role=Role.RESELLER.value,
                referral_code="RS950020",
            )
            customer = BotUser(
                telegram_id=950021,
                username="cust",
                full_name="Customer",
                role=Role.USER.value,
                referral_code="RS950021",
            )
            session.add_all([user, customer])
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="rs_own_pg",
                is_active=True,
            )
            session.add(profile)
            await session.flush()
            principal = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=DEPTH_ONE,
                pg_username="rs_own_pg",
                reseller_profile_id=int(profile.id),
                bot_user_id=int(user.id),
                status=STATUS_ACTIVE,
            )
            customer.owner_principal_id = int(principal.id)
            customer.reseller_id = int(user.id)
            session.add(
                OrgPrincipalWebIdentity(
                    principal_id=int(principal.id),
                    web_username="rs_own_web",
                    web_password_hash=hash_password("Aa1!aaaa"),
                    is_active=True,
                )
            )
            await session.commit()
            uid = int(user.id)
            cust_id = int(customer.id)
            principal_id = int(principal.id)

            mock_pg = AsyncMock()
            mock_pg.delete_admin = AsyncMock(return_value=None)
            with patch("app.services.pasarguard.get_pg", return_value=mock_pg):
                await revoke_reseller(session, uid, delete_pg_admin=False)

            cust = await session.get(BotUser, cust_id)
            self.assertIsNone(cust.owner_principal_id)
            self.assertIsNone(cust.reseller_id)
            self.assertIsNone(await session.get(OrgPrincipal, principal_id))
            n_ident = await session.scalar(
                select(func.count())
                .select_from(OrgPrincipalWebIdentity)
                .where(OrgPrincipalWebIdentity.principal_id == principal_id)
            )
            self.assertEqual(int(n_ident or 0), 0)

    async def test_revoke_web_access_clears_org_principal_and_panel_ticket(self):
        from app.db.models import (
            OrgPrincipal,
            PanelTicket,
            PanelTicketPriority,
            PanelTicketStatus,
            PgStaffAccess,
        )
        from app.services.org_principals import (
            DEPTH_ONE,
            STATUS_ACTIVE,
            create_principal,
        )
        from app.services.pg_staff_access import revoke_web_access
        from app.services.web_auth import hash_password

        async with self.Session() as session:
            owner = await self._seed_owner(session)
            staff = PgStaffAccess(
                pg_username="staff_fk_pg",
                web_username="staff_fk_web",
                web_password_hash=hash_password("Aa1!aaaa"),
                is_active=True,
            )
            session.add(staff)
            await session.flush()
            principal = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=DEPTH_ONE,
                pg_username="staff_fk_pg",
                pg_staff_id=int(staff.id),
                status=STATUS_ACTIVE,
            )
            ticket = PanelTicket(
                subject="help",
                status=PanelTicketStatus.OPEN.value,
                priority=PanelTicketPriority.NORMAL.value,
                opener_role="pg_staff",
                opener_pg_staff_id=int(staff.id),
                opener_label="staff",
            )
            session.add(ticket)
            await session.commit()
            staff_id = int(staff.id)
            principal_id = int(principal.id)
            ticket_id = int(ticket.id)

            ok = await revoke_web_access(session, "staff_fk_pg", commit=True)
            self.assertTrue(ok)
            self.assertIsNone(await session.get(PgStaffAccess, staff_id))
            self.assertIsNone(await session.get(OrgPrincipal, principal_id))
            t = await session.get(PanelTicket, ticket_id)
            self.assertIsNotNone(t)
            self.assertIsNone(t.opener_pg_staff_id)

    def test_revoke_reseller_source_calls_principal_purge(self):
        from pathlib import Path

        src = Path("app/services/resellers.py").read_text(encoding="utf-8")
        block = src.split("async def revoke_reseller", 1)[1].split(
            "\ndef format_revoke_message", 1
        )[0]
        self.assertIn("purge_principal_for_reseller_profile", block)
        self.assertLess(
            block.find("purge_principal_for_reseller_profile"),
            block.find("await session.delete(profile)"),
        )

    def test_revoke_web_access_source_calls_principal_purge(self):
        from pathlib import Path

        src = Path("app/services/pg_staff_access.py").read_text(encoding="utf-8")
        revoke_block = src.split("async def revoke_web_access", 1)[1].split(
            "\nasync def set_active", 1
        )[0]
        purge_block = src.split("async def purge_orphaned_staff_access", 1)[1].split(
            "\n@dataclass", 1
        )[0]
        self.assertIn("_detach_pg_staff_fk_deps", revoke_block)
        self.assertIn("_detach_pg_staff_fk_deps", purge_block)
        self.assertLess(
            revoke_block.find("_detach_pg_staff_fk_deps"),
            revoke_block.find("await session.delete(row)"),
        )
        helper = src.split("async def _detach_pg_staff_fk_deps", 1)[1].split(
            "\nasync def purge_orphaned_staff_access", 1
        )[0]
        self.assertIn("purge_principal_for_pg_staff", helper)
        self.assertIn("opener_pg_staff_id", helper)


if __name__ == "__main__":
    unittest.main()
