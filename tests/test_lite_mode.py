"""«حالت سبک»: only the owner switches it; it spaces out the housekeeping jobs and never a payment, delivery or
notice job; it never makes a job run more often than its own timer; pressing twice changes nothing; switching it
off puts every job back; and a panel restarted with it on spaces the jobs out again."""

from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Setting, ShopStaffAction
from app.jobs import scheduler as scheduler_module
from app.services import job_trigger
from app.services.lite_mode import LITE_MODE_KEY, RELAXED_JOBS, apply_lite_mode
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

# Jobs whose lateness a customer or a shop would feel in money, delivery or a message.
TIMED_JOBS = (
    "billing_tick", "credit_cycle", "cashback_mature", "pg_admin_subscription", "scheduled_receipt_approval",
    "crypto_rate_refresh", "scheduled_backup", "bbc_settlement_poll", "psp_settlement_poll", "paid_order_recovery",
    "volume_external_sync", "service_freezes", "service_transfers", "switch_reverts", "cancellation_notifications",
    "customer_notices", "targeted_campaigns", "broadcast", "service_automation", "expiry", "admin_daily_report",
    "connection_status", "pg_link_watchdog",
)


async def _noop() -> None:
    return None


class SchedulerCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.scheduler.start(paused=True)
        self.addCleanup(self.scheduler.shutdown, wait=False)
        for name, value in (("scheduler", self.scheduler), ("_normal_intervals", {})):
            patcher = patch.object(scheduler_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        job_trigger.register_job_interval_setter(scheduler_module._set_job_interval)
        self.addCleanup(job_trigger.register_job_interval_setter, None)

    def add(self, job_id: str, seconds: int) -> None:
        self.scheduler.add_job(_noop, "interval", seconds=seconds, id=job_id)

    def interval(self, job_id: str) -> int:
        return int(self.scheduler.get_job(job_id).trigger.interval.total_seconds())


class SpacingTests(SchedulerCase):
    def test_no_payment_delivery_or_notice_job_is_ever_spaced_out(self) -> None:
        self.assertEqual(set(RELAXED_JOBS) & set(TIMED_JOBS), set())

    async def test_lite_mode_spaces_the_housekeeping_jobs_and_leaves_the_others(self) -> None:
        self.add("service_cleanup", 1800)
        self.add("panel_alert_notifications", 300)
        self.add("psp_settlement_poll", 45)
        self.assertEqual(apply_lite_mode(True), 2)
        self.assertEqual(self.interval("service_cleanup"), RELAXED_JOBS["service_cleanup"])
        self.assertEqual(self.interval("panel_alert_notifications"), RELAXED_JOBS["panel_alert_notifications"])
        self.assertEqual(self.interval("psp_settlement_poll"), 45)

    async def test_switching_off_puts_every_job_back_and_twice_changes_nothing(self) -> None:
        self.add("service_cleanup", 1800)
        for _ in range(2):
            apply_lite_mode(True)
        self.assertEqual(self.interval("service_cleanup"), RELAXED_JOBS["service_cleanup"])
        for _ in range(2):
            apply_lite_mode(False)
        self.assertEqual(self.interval("service_cleanup"), 1800)

    async def test_a_job_is_never_run_more_often_than_its_own_timer(self) -> None:
        self.add("chat_message_purge", 5 * 24 * 3600)
        apply_lite_mode(True)
        self.assertEqual(self.interval("chat_message_purge"), 5 * 24 * 3600)

    async def test_a_job_the_bot_adds_again_is_spaced_out_again(self) -> None:
        self.add("panel_alert_notifications", 300)
        apply_lite_mode(True)
        self.scheduler.add_job(_noop, "interval", seconds=300, id="panel_alert_notifications", replace_existing=True)
        apply_lite_mode(True)
        self.assertEqual(self.interval("panel_alert_notifications"), RELAXED_JOBS["panel_alert_notifications"])
        apply_lite_mode(False)
        self.assertEqual(self.interval("panel_alert_notifications"), 300)

    def test_without_a_running_scheduler_nothing_changes(self) -> None:
        job_trigger.register_job_interval_setter(None)
        self.assertEqual(apply_lite_mode(True), 0)


class LiteModeRouteTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.applied: list[tuple[str, int | None]] = []
        job_trigger.register_job_interval_setter(lambda job_id, seconds: self.applied.append((job_id, seconds)) or True)
        self.addCleanup(job_trigger.register_job_interval_setter, None)

    async def call(self, session_payload: dict, method: str, path: str, *, csrf: bool = True):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "origin": "http://panel.test"}
        if csrf:
            headers["x-csrf-token"] = "token"
        return await self.client.request(method, path, headers=headers)

    async def saved(self) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == LITE_MODE_KEY))

    async def actions(self) -> list[str]:
        async with self.Session() as session:
            return [row.reason for row in await session.scalars(select(ShopStaffAction).order_by(ShopStaffAction.id))]

    async def test_the_owner_turns_it_on_and_off_and_each_press_is_recorded(self) -> None:
        for _ in range(2):
            response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/lite-mode/on")
            self.assertIn("/tools/maintenance?ok=", response.headers["location"])
        self.assertEqual(await self.saved(), "1")
        self.assertEqual({seconds for _, seconds in self.applied}, set(RELAXED_JOBS.values()))
        self.applied.clear()
        await self.call(OWNER_SESSION, "POST", "/tools/maintenance/lite-mode/off")
        self.assertEqual(await self.saved(), "0")
        self.assertEqual({job for job, _ in self.applied}, set(RELAXED_JOBS))
        self.assertEqual({seconds for _, seconds in self.applied}, {None})
        self.assertEqual(await self.actions(), ["روشن کردن حالت سبک", "روشن کردن حالت سبک", "خاموش کردن حالت سبک"])

    async def test_the_row_offers_the_opposite_of_the_saved_state(self) -> None:
        page = (await self.call(OWNER_SESSION, "GET", "/tools/maintenance")).text
        self.assertIn('action="/tools/maintenance/lite-mode/on"', page)
        await self.call(OWNER_SESSION, "POST", "/tools/maintenance/lite-mode/on")
        page = (await self.call(OWNER_SESSION, "GET", "/tools/maintenance")).text
        self.assertIn('action="/tools/maintenance/lite-mode/off"', page)
        self.assertNotIn('action="/tools/maintenance/lite-mode/on"', page)

    async def test_a_shop_or_a_post_without_the_csrf_token_changes_nothing(self) -> None:
        response = await self.call(SHOP_SESSION, "POST", "/tools/maintenance/lite-mode/on")
        self.assertEqual(response.headers["location"], "/home")
        response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/lite-mode/on", csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(await self.saved())
        self.assertEqual(self.applied, [])
        self.assertEqual(await self.actions(), [])

    async def test_after_a_restart_the_saved_switch_is_applied_again(self) -> None:
        with patch.object(scheduler_module, "SessionLocal", self.Session):
            await scheduler_module.run_lite_mode_apply()
            self.assertEqual(self.applied, [])
            await self.call(OWNER_SESSION, "POST", "/tools/maintenance/lite-mode/on")
            self.applied.clear()
            await scheduler_module.run_lite_mode_apply()
        self.assertEqual({job for job, _ in self.applied}, set(RELAXED_JOBS))



class StartupTests(SchedulerCase):
    async def test_a_database_that_cannot_be_read_leaves_the_jobs_on_their_own_timers(self) -> None:
        self.add("service_cleanup", 1800)
        with patch.object(scheduler_module, "SessionLocal", side_effect=OSError("db down")):
            await scheduler_module.run_lite_mode_apply()
        self.assertEqual(self.interval("service_cleanup"), 1800)
