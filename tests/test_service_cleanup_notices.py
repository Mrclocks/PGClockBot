"""Customers are warned once per stage, before deletion, only while the panel confirms the service is still over."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, CustomerNotice, ResellerProfile, UserService
from app.services import service_cleanup as cleanup
from app.services import service_cleanup_notices as notices
from app.services.pasarguard import PasarGuardError
from app.services.customer_action_notices import CLEANUP_NOTICE_TEXTS, deliver_notice, notice_text
from app.services.service_cleanup import KIND_TIME, KIND_VOLUME, CleanupPolicy
from app.services.users import clear_settings_cache

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SHOP = 5
ENABLED = NOW - timedelta(days=60)


def policy(**kw) -> CleanupPolicy:
    base = dict(time_on=True, time_days=7, warn_first=3, warn_last=1, time_enabled_at=ENABLED,
                volume_on=True, volume_days=30, volume_enabled_at=ENABLED)
    return CleanupPolicy(**{**base, **kw})


class FakePanel:
    """Answers get_user_by_id from a per-user dict: a user dict, or an exception to raise."""

    def __init__(self, answers: dict[int, object]) -> None:
        self.answers = answers
        self.calls: list[int] = []

    async def get_user_by_id(self, user_id: int):
        self.calls.append(user_id)
        answer = self.answers[user_id]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def user_info(*, expire: datetime | None = None, status: str = "expired", data_limit: int = 100) -> dict:
    return {"status": status, "expire": int(expire.timestamp()) if expire else 0, "data_limit": data_limit, "used_traffic": 0}


class NoticeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'n.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="s6", role="reseller"),
                BotUser(id=20, telegram_id=120, referral_code="c20", reseller_id=SHOP),
                BotUser(id=21, telegram_id=121, referral_code="c21", reseller_id=6),
                BotUser(id=22, telegram_id=122, referral_code="c22", is_blocked=True, reseller_id=SHOP),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()

    async def add(self, service_id: int, owner: int = 20, shop: int | None = SHOP, **fields) -> None:
        async with self.Session() as session:
            session.add(UserService(id=service_id, bot_user_id=owner, pg_user_id=1000 + service_id, pg_username=f"svc{service_id}",
                                    shop_id=shop, **fields))
            await session.commit()

    async def scan(self, panel: FakePanel, *, shop: int | None = SHOP, kind: str = KIND_TIME, pol: CleanupPolicy | None = None,
                   at: datetime = NOW, limit: int = 100):
        with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)):
            async with self.Session() as session:
                return await notices.scan_notices(session, shop_id=shop, policy=pol or policy(), kind=kind, now=at, limit=limit)

    async def queued(self) -> list[CustomerNotice]:
        async with self.Session() as session:
            return list((await session.scalars(select(CustomerNotice).order_by(CustomerNotice.id))).all())

    async def stage(self, service_id: int, volume: bool = False) -> int:
        async with self.Session() as session:
            row = await session.get(UserService, service_id)
            return row.cleanup_volume_warn_stage if volume else row.cleanup_warn_stage


class TimeStageTests(NoticeCase):
    async def test_an_expired_service_gets_the_notice_with_its_deadline_once(self) -> None:
        expire = NOW - timedelta(days=1)
        await self.add(1, quota_expire_at=expire)
        panel = FakePanel({1001: user_info(expire=expire)})
        await self.scan(panel)
        await self.scan(panel)
        rows = await self.queued()
        self.assertEqual([(r.action, r.service_id, r.shop_id, r.telegram_id) for r in rows], [("cleanup_expired", 1, SHOP, 120)])
        self.assertEqual(rows[0].reason, (expire + timedelta(days=7)).strftime("%Y-%m-%d"))
        self.assertEqual(await self.stage(1), 1)

    async def test_each_warning_is_sent_when_its_day_comes_and_not_again(self) -> None:
        expire = NOW - timedelta(days=1)
        await self.add(1, quota_expire_at=expire)
        panel = FakePanel({1001: user_info(expire=expire)})
        await self.scan(panel, at=NOW)                       # 6 days left: notice
        await self.scan(panel, at=NOW + timedelta(days=3))   # 3 days left: first warning
        await self.scan(panel, at=NOW + timedelta(days=3))   # repeat: nothing
        await self.scan(panel, at=NOW + timedelta(days=5))   # 1 day left: last warning
        await self.scan(panel, at=NOW + timedelta(days=5))
        self.assertEqual([r.action for r in await self.queued()], ["cleanup_expired", "cleanup_warning", "cleanup_warning"])
        self.assertEqual(await self.stage(1), 3)

    async def test_after_downtime_only_the_latest_due_stage_is_sent(self) -> None:
        expire = NOW - timedelta(days=6)
        await self.add(1, quota_expire_at=expire)
        await self.scan(FakePanel({1001: user_info(expire=expire)}), at=NOW)  # 1 day left
        rows = await self.queued()
        self.assertEqual([(r.action, await self.stage(1)) for r in rows], [("cleanup_warning", 3)])

    async def test_a_service_renewed_in_the_panel_is_not_warned_and_its_stage_resets(self) -> None:
        await self.add(1, quota_expire_at=NOW - timedelta(days=5), cleanup_warn_stage=2)
        renewed = user_info(expire=NOW + timedelta(days=30), status="active")
        await self.scan(FakePanel({1001: renewed}))
        self.assertEqual(await self.queued(), [])
        self.assertEqual(await self.stage(1), 0)

    async def test_an_unreadable_panel_changes_nothing(self) -> None:
        await self.add(1, quota_expire_at=NOW - timedelta(days=2), cleanup_warn_stage=1)
        for failure in (TimeoutError(), ValueError("boom")):
            await self.scan(FakePanel({1001: failure}))
        self.assertEqual(await self.queued(), [])
        self.assertEqual(await self.stage(1), 1)

    async def test_switching_the_rule_on_today_gives_an_old_backlog_the_full_grace(self) -> None:
        expire = NOW - timedelta(days=100)
        await self.add(1, quota_expire_at=expire)
        fresh = policy(time_enabled_at=NOW)
        await self.scan(FakePanel({1001: user_info(expire=expire)}), pol=fresh)
        rows = await self.queued()
        self.assertEqual(rows[0].reason, (NOW + timedelta(days=7)).strftime("%Y-%m-%d"))

    async def test_an_expiring_service_that_is_not_expired_yet_is_ignored(self) -> None:
        await self.add(1, quota_expire_at=NOW + timedelta(days=2))
        panel = FakePanel({1001: user_info(expire=NOW + timedelta(days=2), status="active")})
        await self.scan(panel)
        self.assertEqual((await self.queued(), panel.calls), ([], []))

    async def test_a_warning_that_is_not_earlier_than_the_rule_days_is_skipped(self) -> None:
        expire = NOW - timedelta(days=1)
        await self.add(1, quota_expire_at=expire)
        short = policy(time_days=2, warn_first=3, warn_last=0)
        await self.scan(FakePanel({1001: user_info(expire=expire)}), pol=short, at=NOW + timedelta(hours=1))
        self.assertEqual([r.action for r in await self.queued()], ["cleanup_expired"])


class RecheckTests(NoticeCase):
    async def test_a_warned_service_is_read_again_only_after_the_recheck_period(self) -> None:
        expire = NOW - timedelta(days=5)
        await self.add(1, quota_expire_at=expire, cleanup_warn_stage=2, quota_synced_at=NOW - timedelta(hours=1))
        panel = FakePanel({1001: user_info(expire=expire)})
        await self.scan(panel)
        self.assertEqual(panel.calls, [])
        await self.scan(panel, at=NOW + timedelta(hours=13))
        self.assertEqual(panel.calls, [1001])


class SkipTests(NoticeCase):
    async def test_exempt_cancelled_blocked_and_unreviewed_services_are_never_warned(self) -> None:
        old = NOW - timedelta(days=2)
        await self.add(1, quota_expire_at=old, cleanup_exempt=True)
        await self.add(3, quota_expire_at=old, is_cancelled=True)
        await self.add(4, owner=22, quota_expire_at=old)  # blocked customer
        await self.add(5, quota_expire_at=old, shop_needs_review=True)
        await self.add(6, quota_expire_at=old, cancellation_pending=True)
        panel = FakePanel({1000 + i: user_info(expire=old) for i in range(1, 7) if i != 2})
        await self.scan(panel)
        self.assertEqual((await self.queued(), panel.calls), ([], []))

    async def test_a_scope_only_reaches_its_own_services_and_a_rule_that_is_off_does_nothing(self) -> None:
        old = NOW - timedelta(days=2)
        await self.add(1, owner=21, shop=6, quota_expire_at=old)
        await self.add(2, owner=20, shop=None, quota_expire_at=old)
        panel = FakePanel({1001: user_info(expire=old), 1002: user_info(expire=old)})
        await self.scan(panel, shop=SHOP)
        self.assertEqual((await self.queued(), panel.calls), ([], []))
        await self.scan(panel, shop=SHOP, pol=CleanupPolicy())
        self.assertEqual(panel.calls, [])
        await self.scan(panel, shop=6)
        self.assertEqual([(r.shop_id, r.service_id) for r in await self.queued()], [(6, 1)])

    async def test_the_batch_is_bounded(self) -> None:
        old = NOW - timedelta(days=2)
        for i in range(1, 6):
            await self.add(i, quota_expire_at=old)
        panel = FakePanel({1000 + i: user_info(expire=old) for i in range(1, 6)})
        result = await self.scan(panel, limit=2)
        self.assertEqual((result.queued, len(panel.calls)), (2, 2))


class PanelRemovedTests(NoticeCase):
    async def test_services_the_panel_already_removed_cannot_stop_the_warnings_for_the_rest(self) -> None:
        old = NOW - timedelta(days=2)
        for i in range(1, 4):
            await self.add(i, quota_expire_at=old, panel_missing_at=NOW - timedelta(days=1))
        await self.add(4, quota_expire_at=old)
        missing = PasarGuardError("not found", 404)
        panel = FakePanel({1001: missing, 1002: missing, 1003: missing, 1004: user_info(expire=old)})
        result = await self.scan(panel, limit=3)
        self.assertEqual(result.queued, 1)
        self.assertEqual(panel.calls, [1004])
        self.assertEqual([r.service_id for r in await self.queued()], [4])


class VolumeStageTests(NoticeCase):
    async def test_a_volume_finished_service_has_its_own_notice_and_stage(self) -> None:
        since = NOW - timedelta(days=2)
        await self.add(1, volume_exhausted_at=since, quota_expire_at=NOW + timedelta(days=20))
        panel = FakePanel({1001: user_info(expire=NOW + timedelta(days=20), status="limited")})
        await self.scan(panel, kind=KIND_VOLUME)
        rows = await self.queued()
        self.assertEqual([r.action for r in rows], ["cleanup_volume_finished"])
        self.assertEqual(rows[0].reason, (since + timedelta(days=30)).strftime("%Y-%m-%d"))
        self.assertEqual((await self.stage(1, volume=True), await self.stage(1)), (1, 0))

    async def test_a_service_that_got_volume_back_is_not_warned_and_its_stage_resets(self) -> None:
        await self.add(1, volume_exhausted_at=NOW - timedelta(days=28), cleanup_volume_warn_stage=2)
        await self.scan(FakePanel({1001: user_info(expire=NOW + timedelta(days=20), status="active", data_limit=500)}), kind=KIND_VOLUME)
        self.assertEqual(await self.queued(), [])
        self.assertEqual(await self.stage(1, volume=True), 0)


class RenewalResetTests(unittest.TestCase):
    def test_a_later_expiry_resets_the_time_stage_and_regained_volume_the_volume_stage(self) -> None:
        from app.services.bot_user_admin import sync_service_quota_cache

        svc = UserService(id=1, bot_user_id=1, pg_user_id=1, pg_username="u", cleanup_warn_stage=3, cleanup_volume_warn_stage=2,
                          quota_expire_at=NOW - timedelta(days=5), volume_exhausted_at=NOW, quota_status="limited")
        sync_service_quota_cache(svc, info={"status": "active", "expire": int((NOW + timedelta(days=30)).timestamp()), "data_limit": 10})
        self.assertEqual((svc.cleanup_warn_stage, svc.cleanup_volume_warn_stage), (0, 0))


    def test_a_small_extension_that_leaves_the_service_expired_keeps_its_stage(self) -> None:
        from app.services.bot_user_admin import sync_service_quota_cache

        svc = UserService(id=1, bot_user_id=1, pg_user_id=1, pg_username="u", cleanup_warn_stage=2, quota_expire_at=NOW - timedelta(days=5))
        sync_service_quota_cache(svc, info={"status": "expired", "expire": int((NOW - timedelta(days=5) + timedelta(hours=3)).timestamp()), "data_limit": 10})
        self.assertEqual(svc.cleanup_warn_stage, 2)


class HoldCandidateTests(NoticeCase):
    async def test_services_held_far_away_are_not_read_from_the_panel_again_and_again(self) -> None:
        expire = NOW - timedelta(days=20)
        for i in range(1, 4):
            await self.add(i, quota_expire_at=expire, cleanup_hold_until=NOW + timedelta(days=30), cleanup_warn_stage=1,
                           quota_synced_at=NOW - timedelta(hours=1))
        await self.add(4, quota_expire_at=expire, cleanup_hold_until=NOW + timedelta(days=2), cleanup_warn_stage=1,
                       quota_synced_at=NOW - timedelta(hours=1))
        panel = FakePanel({1000 + i: user_info(expire=expire) for i in range(1, 5)})
        await self.scan(panel, limit=100)
        self.assertEqual(panel.calls, [1004])  # only the one whose hold ends inside the warning window


class HoldTests(NoticeCase):
    async def test_a_held_service_is_warned_before_its_new_deadline_with_the_hold_date(self) -> None:
        expire = NOW - timedelta(days=20)
        hold = NOW + timedelta(days=2)
        await self.add(1, quota_expire_at=expire, cleanup_hold_until=hold, cleanup_warn_stage=1)
        await self.scan(FakePanel({1001: user_info(expire=expire)}))
        rows = await self.queued()
        self.assertEqual([(r.action, r.reason) for r in rows], [("cleanup_warning", hold.strftime("%Y-%m-%d"))])
        self.assertEqual(await self.stage(1), 2)

    async def test_a_service_held_far_into_the_future_is_not_warned_yet(self) -> None:
        expire = NOW - timedelta(days=20)
        await self.add(1, quota_expire_at=expire, cleanup_hold_until=NOW + timedelta(days=30), cleanup_warn_stage=1)
        await self.scan(FakePanel({1001: user_info(expire=expire)}))
        self.assertEqual(await self.queued(), [])

    async def test_the_time_of_the_last_warning_is_recorded(self) -> None:
        expire = NOW - timedelta(days=1)
        await self.add(1, quota_expire_at=expire)
        await self.scan(FakePanel({1001: user_info(expire=expire)}))
        async with self.Session() as session:
            warned = (await session.get(UserService, 1)).cleanup_warned_at
        self.assertEqual(warned.replace(tzinfo=timezone.utc), NOW)


class NoticeTextTests(NoticeCase):
    def test_every_cleanup_text_escapes_the_service_name_and_shows_the_date(self) -> None:
        for action in CLEANUP_NOTICE_TEXTS:
            text = notice_text(action, service_name="<script>x</script>", reason="2026-11-17")
            self.assertNotIn("<script>", text, action)
            if not action.endswith("deleted"):  # the deleted notice has no deadline left to show
                self.assertIn("2026-11-17", text, action)

    async def test_a_cleanup_warning_carries_a_renew_button_and_other_notices_do_not(self) -> None:
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, **kwargs):
                sent.append(kwargs.get("reply_markup"))

        async def bot_for(_shop):
            return Bot()

        async with self.Session() as session:
            for action in ("cleanup_warning", "service_renew"):
                row = CustomerNotice(id=1, shop_id=SHOP, bot_user_id=20, telegram_id=120, action=action, service_name="s",
                                     reason="2026-11-17", service_id=7)
                await deliver_notice(session, bot_for, row)
        self.assertEqual(sent[0].inline_keyboard[0][0].callback_data, "svc:renew:7")
        self.assertIsNone(sent[1])


class ScopesTests(NoticeCase):
    async def test_only_active_shops_with_a_rule_on_are_scanned_and_the_platform_comes_first(self) -> None:
        async with self.Session() as session:
            session.add_all([ResellerProfile(user_id=SHOP, is_active=True), ResellerProfile(user_id=6, is_active=False)])
            await session.commit()
            await cleanup.save_policy(session, shop_id=None, policy=cleanup.parse_policy_form(
                {"time_on": "1", "time_days": "7", "volume_days": "30", "warn_first": "3", "warn_last": "1"}), now=NOW)
            for shop in (SHOP, 6):
                await cleanup.save_policy(session, shop_id=shop, policy=cleanup.parse_policy_form(
                    {"volume_on": "1", "time_days": "7", "volume_days": "30", "warn_first": "3", "warn_last": "1"}), now=NOW)
            scopes = await cleanup.enabled_scopes(session)
        self.assertEqual([shop for shop, _ in scopes], [None, SHOP])


if __name__ == "__main__":
    unittest.main()


class VolumeStageMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        import importlib.util

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0107_cleanup_volume_warn_stage.py"
        spec = importlib.util.spec_from_file_location("m0099", path)
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("INSERT INTO user_services (id) VALUES (1)"))
            for down in (False, False, True, True):
                with Operations.context(MigrationContext.configure(conn)):
                    (migration.downgrade if down else migration.upgrade)()
                has = "cleanup_volume_warn_stage" in {c["name"] for c in sa.inspect(conn).get_columns("user_services")}
                self.assertEqual(has, not down)
            self.assertEqual(conn.execute(sa.text("SELECT id FROM user_services")).scalar(), 1)


class WarnedAtMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        import importlib.util

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0108_cleanup_warned_at.py"
        spec = importlib.util.spec_from_file_location("m0100", path)
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, cleanup_warn_stage INTEGER, cleanup_volume_warn_stage INTEGER)"))
            conn.execute(sa.text("INSERT INTO user_services VALUES (1, 0, 0), (2, 2, 0), (3, 0, 1)"))
            for down in (False, False, True, True):
                with Operations.context(MigrationContext.configure(conn)):
                    (migration.downgrade if down else migration.upgrade)()
                has = "cleanup_warned_at" in {c["name"] for c in sa.inspect(conn).get_columns("user_services")}
                self.assertEqual(has, not down)
                if not down and not has:
                    continue
            self.assertEqual(conn.execute(sa.text("SELECT COUNT(*) FROM user_services")).scalar(), 3)

    def test_services_already_warned_get_a_warning_date_and_the_others_stay_empty(self) -> None:
        import importlib.util

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0108_cleanup_warned_at.py"
        spec = importlib.util.spec_from_file_location("m0100b", path)
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, cleanup_warn_stage INTEGER, cleanup_volume_warn_stage INTEGER)"))
            conn.execute(sa.text("INSERT INTO user_services VALUES (1, 0, 0), (2, 2, 0), (3, 0, 1)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
            dated = {row[0]: row[1] is not None for row in conn.execute(sa.text("SELECT id, cleanup_warned_at FROM user_services"))}
        self.assertEqual(dated, {1: False, 2: True, 3: True})
