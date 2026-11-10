"""The expired-service cleanup rule: validation, per-shop isolation, grace after switching on, preview."""

from __future__ import annotations

import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerProfile, UserService
from app.services import service_cleanup as cleanup
from app.services.service_cleanup import CleanupPolicy, CleanupPolicyError
from app.services.users import clear_settings_cache
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SHOP = 5
OTHER_SHOP = 6


def form(**values: str) -> dict[str, str]:
    return {"time_on": "1", "time_days": "7", "warn_first": "3", "warn_last": "1", "volume_on": "", "volume_days": "30", **values}


class ParsePolicyTests(unittest.TestCase):
    def test_a_valid_form_becomes_a_policy(self) -> None:
        policy = cleanup.parse_policy_form(form(volume_on="1", volume_days="45"))
        self.assertEqual((policy.time_on, policy.time_days, policy.warn_first, policy.warn_last), (True, 7, 3, 1))
        self.assertEqual((policy.volume_on, policy.volume_days), (True, 45))

    def test_out_of_range_and_non_numeric_values_are_refused(self) -> None:
        for bad in ({"time_days": "0"}, {"time_days": "366"}, {"time_days": "-3"}, {"time_days": "abc"},
                    {"time_days": ""}, {"time_days": "7.5"}, {"time_days": "99999999"}, {"volume_days": "0"},
                    {"warn_first": "x"}, {"warn_last": "-1"}):
            with self.assertRaises(CleanupPolicyError, msg=bad):
                cleanup.parse_policy_form(form(**bad))

    def test_warnings_must_come_before_the_deletion_and_in_order(self) -> None:
        for bad in ({"warn_first": "7"}, {"warn_last": "8"}, {"warn_first": "2", "warn_last": "2"}, {"warn_first": "1", "warn_last": "3"}):
            with self.assertRaises(CleanupPolicyError, msg=bad):
                cleanup.parse_policy_form(form(**bad))
        self.assertEqual(cleanup.parse_policy_form(form(warn_first="0", warn_last="0")).warning_days(), [])

    def test_a_policy_from_broken_settings_falls_back_to_safe_defaults(self) -> None:
        policy = cleanup.policy_from_settings({cleanup.KEY_TIME_DAYS: "-4", cleanup.KEY_VOLUME_DAYS: "zzz", cleanup.KEY_TIME_ON: "1"})
        self.assertEqual((policy.time_days, policy.volume_days), (7, 30))
        self.assertIsNone(policy.time_enabled_at)


class CleanupCase(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add_all([
                BotUser(id=6, telegram_id=106, referral_code="shop6", role="reseller"),
                BotUser(id=20, telegram_id=120, referral_code="c20", full_name="<b>Eve</b>", reseller_id=SHOP),
                BotUser(id=21, telegram_id=121, referral_code="c21", reseller_id=OTHER_SHOP),
                BotUser(id=22, telegram_id=122, referral_code="c22"),
            ])
            await session.commit()

    async def add_service(self, service_id: int, owner: int, shop: int | None, **fields) -> None:
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add(UserService(id=service_id, bot_user_id=owner, pg_user_id=1000 + service_id,
                                    pg_username=fields.pop("pg_username", f"svc{service_id}"), shop_id=shop, **fields))
            await session.commit()

    async def session(self) -> AsyncSession:
        return AsyncSession(bind=self.engine, expire_on_commit=False)

    async def set_shop_permissions(self, raw: str | None) -> None:
        async with await self.session() as session:
            profile = (await session.scalars(select(ResellerProfile).where(ResellerProfile.user_id == SHOP))).one()
            profile.web_permissions = raw
            profile.bot_permissions = raw
            await session.commit()


class ScopeTests(CleanupCase):
    def test_the_platform_owner_configures_the_platform_and_a_shop_needs_the_permission(self) -> None:
        self.assertIsNone(cleanup.cleanup_scope({**OWNER_SESSION}))
        shop = {"role": "reseller", "bot_user_id": SHOP, "permissions": ["service_cleanup"]}
        self.assertEqual(cleanup.cleanup_scope(shop), SHOP)
        for denied in ({"role": "reseller", "bot_user_id": SHOP, "permissions": ["orders_purge"]},
                       {"role": "reseller", "bot_user_id": SHOP, "permissions": []},
                       {"role": "pg_staff", "permissions": ["service_cleanup"]}):
            with self.assertRaises(PermissionError, msg=denied):
                cleanup.cleanup_scope(denied)


class SavePolicyTests(CleanupCase):
    async def test_the_switch_on_moment_is_kept_while_the_rule_stays_on_and_cleared_when_off(self) -> None:
        on = cleanup.parse_policy_form(form())
        async with await self.session() as session:
            first = await cleanup.save_policy(session, shop_id=SHOP, policy=on, now=NOW)
            again = await cleanup.save_policy(session, shop_id=SHOP, policy=on, now=NOW + timedelta(days=3))
            self.assertEqual((first.time_enabled_at, again.time_enabled_at), (NOW, NOW))
            await cleanup.save_policy(session, shop_id=SHOP, policy=cleanup.parse_policy_form(form(time_on="")), now=NOW)
            self.assertIsNone((await cleanup.load_policy(session, shop_id=SHOP)).time_enabled_at)
            back = await cleanup.save_policy(session, shop_id=SHOP, policy=on, now=NOW + timedelta(days=9))
            self.assertEqual(back.time_enabled_at, NOW + timedelta(days=9))

    async def test_each_shop_keeps_its_own_rule(self) -> None:
        async with await self.session() as session:
            await cleanup.save_policy(session, shop_id=SHOP, policy=cleanup.parse_policy_form(form(time_days="10")), now=NOW)
            self.assertEqual((await cleanup.load_policy(session, shop_id=OTHER_SHOP)).time_on, False)
            self.assertEqual((await cleanup.load_policy(session, shop_id=None)).time_on, False)
            self.assertEqual((await cleanup.load_policy(session, shop_id=SHOP)).time_days, 10)


class PreviewTests(CleanupCase):
    def policy(self, **kw) -> CleanupPolicy:
        base = dict(time_on=True, time_days=7, time_enabled_at=NOW - timedelta(days=30),
                    volume_on=True, volume_days=30, volume_enabled_at=NOW - timedelta(days=60))
        return CleanupPolicy(**{**base, **kw})

    async def preview(self, shop: int | None, policy: CleanupPolicy):
        async with await self.session() as session:
            return await cleanup.build_preview(session, shop_id=shop, policy=policy, now=NOW)

    async def test_counts_split_expired_due_now_and_due_soon(self) -> None:
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=10))   # due now
        await self.add_service(2, 20, SHOP, quota_expire_at=NOW - timedelta(days=5))    # due in 2 days
        await self.add_service(3, 20, SHOP, quota_expire_at=NOW - timedelta(days=1))    # due in 6 days
        await self.add_service(4, 20, SHOP, quota_expire_at=NOW - timedelta(days=0, hours=1))  # due in 7 days minus 1 h
        await self.add_service(5, 20, SHOP, quota_expire_at=NOW + timedelta(days=3))    # not expired
        result = await self.preview(SHOP, self.policy())
        self.assertEqual((result.expired_total, result.time_due_now, result.time_due_soon), (4, 1, 3))

    async def test_another_shop_and_the_platform_are_never_counted(self) -> None:
        await self.add_service(1, 21, OTHER_SHOP, quota_expire_at=NOW - timedelta(days=20))
        await self.add_service(2, 22, None, quota_expire_at=NOW - timedelta(days=20))
        mine = await self.preview(SHOP, self.policy())
        self.assertEqual((mine.expired_total, mine.time_due_now, mine.rows), (0, 0, []))
        self.assertEqual((await self.preview(None, self.policy())).time_due_now, 1)

    async def test_exempt_held_cancelled_pending_and_unreviewed_services_are_skipped(self) -> None:
        old = NOW - timedelta(days=20)
        await self.add_service(1, 20, SHOP, quota_expire_at=old, cleanup_exempt=True)
        await self.add_service(2, 20, SHOP, quota_expire_at=old, cleanup_hold_until=NOW + timedelta(days=2))
        await self.add_service(3, 20, SHOP, quota_expire_at=old, is_cancelled=True)
        await self.add_service(4, 20, SHOP, quota_expire_at=old, cancellation_pending=True)
        await self.add_service(5, 20, SHOP, quota_expire_at=old, shop_needs_review=True)
        await self.add_service(6, 20, SHOP, quota_expire_at=old, cleanup_hold_until=NOW - timedelta(days=1))  # hold passed
        result = await self.preview(SHOP, self.policy())
        # Service 2 is held for 2 more days: it is listed with its hold date, but it is not due now.
        self.assertEqual([r.service_id for r in result.rows], [2, 6])
        self.assertEqual((result.time_due_now, result.time_due_soon), (1, 1))

    async def test_switching_the_rule_on_never_makes_an_old_backlog_due_at_once(self) -> None:
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=100))
        fresh = self.policy(time_enabled_at=NOW - timedelta(days=1))  # switched on yesterday, 7-day grace
        result = await self.preview(SHOP, fresh)
        self.assertEqual((result.expired_total, result.time_due_now, result.time_due_soon), (1, 0, 1))
        self.assertEqual(result.rows[0].deadline, (NOW + timedelta(days=6)).strftime("%Y-%m-%d"))

    async def test_a_held_service_stays_in_the_list_with_its_hold_date_once_the_hold_ends_within_the_window(self) -> None:
        old = NOW - timedelta(days=20)
        await self.add_service(1, 20, SHOP, quota_expire_at=old, cleanup_hold_until=NOW + timedelta(days=3))   # ends in 3 days
        await self.add_service(2, 20, SHOP, quota_expire_at=old, cleanup_hold_until=NOW + timedelta(days=30))  # far away
        result = await self.preview(SHOP, self.policy())
        self.assertEqual([r.service_id for r in result.rows], [1])
        self.assertEqual(result.rows[0].deadline, (NOW + timedelta(days=3)).strftime("%Y-%m-%d"))
        self.assertEqual((result.expired_total, result.time_due_now, result.time_due_soon), (2, 0, 1))

    async def test_a_rule_that_is_off_has_no_due_services(self) -> None:
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=100), volume_exhausted_at=NOW - timedelta(days=100))
        result = await self.preview(SHOP, CleanupPolicy())
        self.assertEqual((result.time_due_now, result.time_due_soon, result.volume_due_now, result.rows), (0, 0, 0, []))
        self.assertEqual((result.expired_total, result.volume_total), (1, 1))

    async def test_volume_finished_services_have_their_own_days(self) -> None:
        await self.add_service(1, 20, SHOP, volume_exhausted_at=NOW - timedelta(days=31))
        await self.add_service(2, 20, SHOP, volume_exhausted_at=NOW - timedelta(days=10))
        result = await self.preview(SHOP, self.policy())
        self.assertEqual((result.volume_total, result.volume_due_now, result.volume_due_soon), (2, 1, 0))

    async def test_the_list_is_bounded_and_ordered_by_the_nearest_deadline(self) -> None:
        for i in range(30):
            await self.add_service(100 + i, 20, SHOP, quota_expire_at=NOW - timedelta(days=8 + i))
        result = await self.preview(SHOP, self.policy())
        self.assertEqual(len(result.rows), cleanup.PREVIEW_SAMPLE)
        self.assertEqual(result.rows[0].service_id, 129)


class CleanupRoutesTests(CleanupCase):
    async def call_as(self, session_payload: dict, method: str, path: str, data: dict | None = None):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, data=data)

    async def test_a_shop_without_the_permission_is_sent_home_and_nothing_is_saved(self) -> None:
        await self.set_shop_permissions("dashboard")
        for method in ("GET", "POST"):
            response = await self.call_as(SHOP_SESSION, method, "/service-cleanup", form() if method == "POST" else None)
            self.assertEqual((response.status_code, response.headers["location"]), (303, "/home"))
        async with await self.session() as session:
            self.assertFalse((await cleanup.load_policy(session, shop_id=SHOP)).time_on)

    async def test_a_shop_with_the_permission_saves_only_its_own_rule(self) -> None:
        await self.set_shop_permissions("dashboard,service_cleanup")
        response = await self.call_as(SHOP_SESSION, "POST", "/service-cleanup", form(time_days="12"))
        self.assertIn("ok=", response.headers["location"])
        async with await self.session() as session:
            self.assertEqual((await cleanup.load_policy(session, shop_id=SHOP)).time_days, 12)
            self.assertEqual((await cleanup.load_policy(session, shop_id=None)).time_days, 7)
            self.assertEqual((await cleanup.load_policy(session, shop_id=OTHER_SHOP)).time_days, 7)

    async def test_an_invalid_form_is_refused_with_a_message_and_saves_nothing(self) -> None:
        await self.set_shop_permissions("dashboard,service_cleanup")
        response = await self.call_as(SHOP_SESSION, "POST", "/service-cleanup", form(time_days="0"))
        self.assertIn("err=", response.headers["location"])
        async with await self.session() as session:
            self.assertFalse((await cleanup.load_policy(session, shop_id=SHOP)).time_on)

    async def test_the_page_shows_only_this_shops_services_and_escapes_customer_text(self) -> None:
        await self.set_shop_permissions("dashboard,service_cleanup")
        async with await self.session() as session:
            await cleanup.save_policy(session, shop_id=SHOP, policy=cleanup.parse_policy_form(form()), now=NOW - timedelta(days=30))
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=20), pg_username="mine<script>")
        await self.add_service(2, 21, OTHER_SHOP, quota_expire_at=NOW - timedelta(days=20), pg_username="foreign")
        response = await self.call_as(SHOP_SESSION, "GET", "/service-cleanup")
        self.assertEqual(response.status_code, 200)
        self.assertIn("mine&lt;script&gt;", response.text)
        self.assertNotIn("mine<script>", response.text)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", response.text)
        self.assertNotIn("foreign", response.text)

    async def test_the_owner_reaches_the_platform_page(self) -> None:
        response = await self.call_as(OWNER_SESSION, "GET", "/service-cleanup")
        self.assertEqual(response.status_code, 200)


class CleanupMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("m0098", ROOT / "alembic/versions/0106_expired_service_cleanup.py")
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        new = set(migration.COLUMNS)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, shop_id INTEGER, quota_expire_at DATETIME)"))
            conn.execute(sa.text("INSERT INTO user_services (id, shop_id) VALUES (1, NULL)"))
            for down in (False, False, True):
                with Operations.context(MigrationContext.configure(conn)):
                    (migration.downgrade if down else migration.upgrade)()
                names = {c["name"] for c in sa.inspect(conn).get_columns("user_services")}
                self.assertEqual(new <= names, not down)
            self.assertEqual(conn.execute(sa.text("SELECT id FROM user_services")).scalar(), 1)


class VolumeFinishedTrackingTests(unittest.TestCase):
    def test_the_moment_the_volume_finished_is_kept_and_cleared_when_it_is_not(self) -> None:
        from app.services.bot_user_admin import sync_service_quota_cache

        svc = UserService(id=1, bot_user_id=1, pg_user_id=1, pg_username="u")
        sync_service_quota_cache(svc, info={"status": "limited", "data_limit": 10})
        first = svc.volume_exhausted_at
        self.assertIsNotNone(first)
        sync_service_quota_cache(svc, info={"status": "limited", "data_limit": 10})
        self.assertEqual(svc.volume_exhausted_at, first)
        sync_service_quota_cache(svc, info={"status": "active", "data_limit": 20})
        self.assertIsNone(svc.volume_exhausted_at)


if __name__ == "__main__":
    unittest.main()
