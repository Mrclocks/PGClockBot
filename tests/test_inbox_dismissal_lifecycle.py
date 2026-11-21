"""Inbox dismissals: new items resurface, stale rows are purged, staff stay isolated."""
from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, PanelInboxDismissal, Plan, UserService
from app.services import inbox_dismissals as mod
from app.services.action_center import build_action_center
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"role": "admin", "org_principal_id": 1}
OTHER_ADMIN = {"role": "admin", "org_principal_id": 2}


def _ctx(pending: int = 0, tickets: int = 0) -> dict:
    entries = []
    if pending:
        entries.append({"key": "pending", "title": "x"})
    if tickets:
        entries.append({"key": "tickets", "title": "y"})
    return {
        "action_center": {
            "pending": pending,
            "tickets": tickets,
            "failures": 0,
            "expiring": 0,
            "low_volume": 0,
            "entries": entries,
            "has_items": bool(entries),
        },
        "action_center_ok": True,
        "payg_risk": {},
    }


class InboxDismissalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _rows(self, session) -> list[PanelInboxDismissal]:
        return list((await session.scalars(select(PanelInboxDismissal))).all())

    async def test_forever_dismiss_resurfaces_when_new_items_arrive(self):
        async with self.sessions() as session:
            await mod.upsert_dismissal(
                session, staff=ADMIN, alert_key="ac:pending", mode=mod.MODE_FOREVER, seen_count=2
            )
            rows = await mod.load_dismissals(session, ADMIN)
            same = mod.filter_inbox_context(_ctx(pending=2), rows)
            self.assertEqual(same["action_center"]["entries"], [])
            grown = mod.filter_inbox_context(_ctx(pending=3), rows)
            self.assertEqual(len(grown["action_center"]["entries"]), 1)

    async def test_cleanup_deletes_rows_for_resolved_or_grown_or_expired_alerts(self):
        async with self.sessions() as session:
            await mod.upsert_dismissal(
                session, staff=ADMIN, alert_key="ac:pending", mode=mod.MODE_FOREVER, seen_count=1
            )
            await mod.upsert_dismissal(
                session, staff=ADMIN, alert_key="ac:tickets", mode=mod.MODE_24H, seen_count=1
            )
            old = (await session.scalars(
                select(PanelInboxDismissal).where(PanelInboxDismissal.alert_key == "ac:tickets")
            )).one()
            old.dismissed_at = datetime.now(timezone.utc) - timedelta(hours=30)
            await session.commit()

            await mod.cleanup_resolved_dismissals(session, ADMIN, _ctx(pending=5, tickets=1))
            self.assertEqual(await self._rows(session), [])

    async def test_cleanup_keeps_active_forever_row(self):
        async with self.sessions() as session:
            await mod.upsert_dismissal(
                session, staff=ADMIN, alert_key="ac:pending", mode=mod.MODE_FOREVER, seen_count=3
            )
            await mod.cleanup_resolved_dismissals(session, ADMIN, _ctx(pending=3))
            self.assertEqual(len(await self._rows(session)), 1)

    async def test_other_staff_cannot_see_or_clear_dismissals(self):
        async with self.sessions() as session:
            await mod.upsert_dismissal(
                session, staff=ADMIN, alert_key="ac:pending", mode=mod.MODE_FOREVER, seen_count=1
            )
            self.assertEqual(await mod.load_dismissals(session, OTHER_ADMIN), [])
            self.assertEqual(await mod.clear_staff_dismissals(session, OTHER_ADMIN), 0)
            self.assertEqual(len(await self._rows(session)), 1)

    async def test_same_username_in_other_role_does_not_share_rows(self):
        reseller = {"role": "reseller", "username": "sam", "bot_user_id": 7}
        admin_sam = {"role": "admin", "username": "sam"}
        async with self.sessions() as session:
            await mod.upsert_dismissal(
                session, staff=reseller, alert_key="ac:pending", mode=mod.MODE_24H
            )
            self.assertEqual(await mod.load_dismissals(session, admin_sam), [])

    async def test_action_center_ignores_cancelled_services(self):
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            user = BotUser(telegram_id=10, role="user", referral_code="r10")
            plan = Plan(name="p", price=1, duration_days=30, data_limit_gb=1)
            session.add_all([user, plan])
            await session.flush()
            for cancelled in (True, False):
                session.add(
                    UserService(
                        bot_user_id=user.id,
                        plan_id=plan.id,
                        pg_username=f"u{cancelled}",
                        is_cancelled=cancelled,
                        notified_traffic=True,
                        quota_synced_at=now,
                        quota_expire_at=now + timedelta(days=1),
                    )
                )
            await session.commit()
            center = await build_action_center(session, reseller_id=None, expire_days=3)
            self.assertEqual(center["low_volume"], 1)
            self.assertEqual(center["expiring"], 1)

    async def test_synced_quota_expiry_beats_creation_estimate(self):
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            user = BotUser(telegram_id=11, role="user", referral_code="r11")
            plan = Plan(name="p", price=1, duration_days=30, data_limit_gb=1)
            session.add_all([user, plan])
            await session.flush()
            session.add(
                UserService(
                    bot_user_id=user.id,
                    plan_id=plan.id,
                    pg_username="renewed",
                    created_at=now - timedelta(days=29),
                    quota_synced_at=now,
                    quota_expire_at=now + timedelta(days=60),
                )
            )
            await session.commit()
            center = await build_action_center(session, reseller_id=None, expire_days=3)
            self.assertEqual(center["expiring"], 0)


class InboxSeenCountMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self):
        spec = importlib.util.spec_from_file_location(
            "seen_count_migration", ROOT / "alembic/versions/0047_inbox_dismiss_seen_count.py"
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(sa.text(
                "CREATE TABLE panel_inbox_dismissals (id INTEGER PRIMARY KEY, alert_key TEXT)"
            ))
            conn.execute(sa.text("INSERT INTO panel_inbox_dismissals VALUES (1, 'update')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(
                    conn.execute(sa.text("SELECT seen_count FROM panel_inbox_dismissals")).scalar(), 0
                )
                migration.downgrade()
                self.assertEqual(
                    conn.execute(sa.text("SELECT alert_key FROM panel_inbox_dismissals")).scalar(),
                    "update",
                )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
