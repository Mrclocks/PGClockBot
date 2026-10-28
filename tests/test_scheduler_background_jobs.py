"""Operational jobs must run before Telegram notification jobs are attached."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.jobs import scheduler as jobs


OPERATIONS = {
    "integration_operations",
    "billing_tick",
    "pg_admin_subscription",
    "pending_order_cleanup",
    "scheduled_backup",
    "connection_status",
    "bbc_settlement_poll",
    "psp_settlement_poll",
    "volume_external_sync",
    "chat_message_purge",
    # Copies each bot's Telegram profile photo for its Mini App logo; needs no running bot.
    "bot_profile_photo",
    # Removes cached customer profile photos for the user card; needs no running bot.
    "user_photo_prune",
}
NOTIFICATIONS = {"service_automation", "expiry", "admin_daily_report", "targeted_campaigns", "cancellation_notifications", "panel_alert_notifications"}


class BackgroundSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        self.patch = patch.object(jobs, "scheduler", self.scheduler)
        self.patch.start()

    async def asyncTearDown(self):
        jobs.stop_scheduler()
        await asyncio.sleep(0)
        self.patch.stop()

    async def test_operational_jobs_start_without_a_bot(self):
        jobs.start_background_scheduler()
        self.assertTrue(self.scheduler.running)
        self.assertEqual({job.id for job in self.scheduler.get_jobs()}, OPERATIONS)
        self.assertTrue(all(not job.args for job in self.scheduler.get_jobs()))

    async def test_repeated_start_preserves_schedules_and_does_not_duplicate_jobs(self):
        jobs.start_background_scheduler()
        next_runs = {job.id: job.next_run_time for job in self.scheduler.get_jobs()}
        jobs.start_background_scheduler()
        self.assertEqual(
            {job.id: job.next_run_time for job in self.scheduler.get_jobs()}, next_runs
        )

    async def test_bot_connection_attaches_notifications_to_running_operations(self):
        jobs.start_background_scheduler()
        next_runs = {job.id: job.next_run_time for job in self.scheduler.get_jobs()}
        bot = object()
        jobs.start_scheduler(bot)
        self.assertEqual(
            {job.id for job in self.scheduler.get_jobs()}, OPERATIONS | NOTIFICATIONS
        )
        for job_id, next_run in next_runs.items():
            self.assertEqual(self.scheduler.get_job(job_id).next_run_time, next_run)
        for job_id in NOTIFICATIONS:
            self.assertEqual(self.scheduler.get_job(job_id).args, (bot,))

    async def test_repeated_bot_registration_updates_recipient_without_duplicates(self):
        jobs.start_scheduler(object())
        bot = object()
        jobs.start_scheduler(bot)
        self.assertEqual(len(self.scheduler.get_jobs()), len(OPERATIONS | NOTIFICATIONS))
        for job_id in NOTIFICATIONS:
            self.assertEqual(self.scheduler.get_job(job_id).args, (bot,))

    async def test_cancellation_notices_run_every_ten_seconds_without_overlapping_batches(self) -> None:
        jobs.start_scheduler(object())
        job = self.scheduler.get_job("cancellation_notifications")
        self.assertEqual(job.trigger.interval.total_seconds(), 10)
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)

    async def test_connection_monitor_runs_each_minute_without_overlapping_batches(self) -> None:
        jobs.start_background_scheduler()
        job = self.scheduler.get_job("connection_status")
        self.assertEqual(job.trigger.interval.total_seconds(), 60)
        self.assertEqual(job.max_instances, 1)
        self.assertTrue(job.coalesce)
        self.assertFalse(job.args)

    async def _check_application_startup(self, *, token):
        from app import main
        from app.config import Settings

        settings = Settings.model_construct(bot_token=token, webhook_url="")
        bot = SimpleNamespace(
            get_me=AsyncMock(side_effect=ConnectionError("Telegram unavailable")),
            session=SimpleNamespace(close=AsyncMock()),
        )
        pg = SimpleNamespace(close=AsyncMock())
        app = None

        def create_app(*, lifespan):
            nonlocal app
            app = FastAPI(lifespan=lifespan)
            return app

        patches = (
            patch.object(main, "get_settings", return_value=settings),
            patch.object(main, "create_bot", return_value=bot),
            patch.object(main, "create_dispatcher", return_value=object()),
            patch.object(main, "create_api_app", side_effect=create_app),
            patch.object(main.uvicorn, "run"),
            patch.object(main, "init_db", AsyncMock()),
            patch.object(main, "seed_demo_plan", AsyncMock()),
            patch.object(main, "get_pg", return_value=pg),
            patch.object(main, "load_web_admin", return_value={"password": "test"}),
            patch("app.services.setup_wizard.ensure_web_secret"),
            patch("app.services.setup_wizard.is_setup_complete", return_value=True),
            patch("app.services.setup_wizard.default_http_panel_url", return_value="http://127.0.0.1:9000"),
            patch("app.services.web_auth.repair_web_admin_from_env", return_value={"password": "test"}),
            patch("app.services.service_control.ensure_restart_helper", return_value=(False, "test")),
            patch("app.services.ssl_certs.uvicorn_ssl_kwargs", return_value={}),
        )
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            main.main()
            self.assertIsNotNone(app)
            async with app.router.lifespan_context(app):
                self.assertTrue(self.scheduler.running)
                self.assertEqual(
                    {job.id for job in self.scheduler.get_jobs()}, OPERATIONS
                )
            await asyncio.sleep(0)
            self.assertFalse(self.scheduler.running)
            pg.close.assert_awaited_once()
            if token:
                bot.get_me.assert_awaited_once()
                bot.session.close.assert_awaited_once()
            else:
                bot.get_me.assert_not_awaited()

    async def test_application_keeps_maintenance_running_when_telegram_is_unavailable(self):
        await self._check_application_startup(token="configured-token")

    async def test_panel_only_application_starts_maintenance_without_a_bot_token(self):
        await self._check_application_startup(token="")
