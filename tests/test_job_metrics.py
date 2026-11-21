"""Run statistics of scheduled background jobs."""

from __future__ import annotations

import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.job_metrics import JobErrorLogHandler, JobMetrics, tracked_job


class JobMetricsTests(unittest.TestCase):
    def test_finished_run_records_duration_and_success(self) -> None:
        metrics = JobMetrics()
        done = datetime(2026, 1, 1, tzinfo=timezone.utc)
        metrics.mark_started("a", now=10.0)
        metrics.mark_finished("a", succeeded=True, now=12.5, finished_at=done)
        (view,) = metrics.snapshot()
        self.assertEqual(view.runs, 1)
        self.assertEqual(view.failures, 0)
        self.assertAlmostEqual(view.last_seconds, 2.5)
        self.assertAlmostEqual(view.average_seconds, 2.5)
        self.assertEqual(view.last_success_at, done)
        self.assertIsNone(view.last_failure_at)

    def test_failed_run_counts_as_failure_and_keeps_last_success(self) -> None:
        metrics = JobMetrics()
        ok_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        metrics.mark_started("a", now=0.0)
        metrics.mark_finished("a", succeeded=True, now=1.0, finished_at=ok_at)
        metrics.mark_started("a", now=5.0)
        metrics.mark_finished("a", succeeded=False, now=8.0, finished_at=ok_at + timedelta(minutes=1))
        (view,) = metrics.snapshot()
        self.assertEqual((view.runs, view.failures), (2, 1))
        self.assertEqual(view.last_success_at, ok_at)
        self.assertAlmostEqual(view.max_seconds, 3.0)
        self.assertAlmostEqual(view.average_seconds, 2.0)

    def test_snapshot_puts_the_busiest_job_first(self) -> None:
        metrics = JobMetrics()
        for job_id, seconds in (("light", 0.1), ("heavy", 9.0), ("medium", 1.0)):
            metrics.mark_started(job_id, now=0.0)
            metrics.mark_finished(job_id, succeeded=True, now=seconds)
        self.assertEqual([v.job_id for v in metrics.snapshot()], ["heavy", "medium", "light"])

    def test_number_of_tracked_jobs_is_bounded(self) -> None:
        metrics = JobMetrics(max_jobs=2)
        for job_id in ("a", "b", "c"):
            metrics.mark_started(job_id, now=0.0)
            metrics.mark_finished(job_id, succeeded=True, now=1.0)
            metrics.mark_overlap(job_id)
        self.assertEqual(sorted(v.job_id for v in metrics.snapshot()), ["a", "b"])

    def test_finish_without_start_counts_a_run_with_zero_duration(self) -> None:
        metrics = JobMetrics()
        metrics.mark_finished("a", succeeded=True, now=100.0)
        (view,) = metrics.snapshot()
        self.assertEqual(view.runs, 1)
        self.assertEqual(view.last_seconds, 0.0)


class SchedulerListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_events_feed_the_registry(self) -> None:
        from app.jobs import scheduler as scheduler_module

        metrics = JobMetrics()
        original = scheduler_module.job_metrics
        scheduler_module.job_metrics = metrics
        sched = AsyncIOScheduler()
        sched.add_listener(scheduler_module.record_job_event)
        release = asyncio.Event()

        async def slow() -> None:
            await release.wait()

        async def broken() -> None:
            raise RuntimeError("boom")

        try:
            sched.start()
            sched.add_job(slow, "interval", seconds=1, id="slow", max_instances=1,
                          next_run_time=datetime.now(timezone.utc))
            sched.add_job(broken, "date", id="broken", run_date=datetime.now(timezone.utc))
            await asyncio.sleep(2.3)  # "slow" is due again while its first run still waits
            release.set()
            await asyncio.sleep(0.2)
        finally:
            sched.shutdown(wait=False)
            scheduler_module.job_metrics = original

        views = {v.job_id: v for v in metrics.snapshot()}
        self.assertEqual(views["broken"].failures, 1)
        self.assertGreaterEqual(views["slow"].overlaps, 1)
        self.assertEqual(views["slow"].runs, 1)
        self.assertGreater(views["slow"].last_seconds, 1.5)


if __name__ == "__main__":
    unittest.main()


class HandledFailureTests(unittest.IsolatedAsyncioTestCase):
    """Jobs catch and log their own errors; such a run still counts as failed."""

    def setUp(self) -> None:
        self.metrics = JobMetrics()
        self.handler = JobErrorLogHandler(self.metrics)
        self.log = logging.getLogger("tests.job_metrics.handled")
        self.log.addHandler(self.handler)
        self.log.propagate = False
        self.addCleanup(self.log.removeHandler, self.handler)

    async def _run(self, job_id: str, body) -> None:
        self.metrics.mark_started(job_id, now=0.0)
        await tracked_job(job_id, body)()
        self.metrics.mark_finished(job_id, succeeded=True, now=1.0)

    async def test_an_error_the_job_logged_marks_its_run_failed(self) -> None:
        async def job() -> None:
            try:
                raise RuntimeError("panel down")
            except RuntimeError:
                self.log.exception("tick failed")

        await self._run("a", job)
        (view,) = self.metrics.snapshot()
        self.assertEqual((view.runs, view.failures), (1, 1))
        self.assertIsNone(view.last_success_at)

    async def test_warnings_and_logs_outside_a_job_do_not_count(self) -> None:
        async def job() -> None:
            self.log.warning("one shop kept its old total")

        await self._run("a", job)
        self.log.error("not inside any job")
        (view,) = self.metrics.snapshot()
        self.assertEqual(view.failures, 0)

    async def test_the_next_clean_run_is_a_success_again(self) -> None:
        async def failing() -> None:
            self.log.error("bad")

        async def clean() -> None:
            return None

        await self._run("a", failing)
        await self._run("a", clean)
        (view,) = self.metrics.snapshot()
        self.assertEqual((view.runs, view.failures), (2, 1))
        self.assertIsNotNone(view.last_success_at)

    async def test_the_wrapper_keeps_the_jobs_name_and_result(self) -> None:
        async def run_sample(value: int) -> int:
            return value * 2

        wrapped = tracked_job("sample", run_sample)
        self.assertEqual(wrapped.__name__, "run_sample")
        self.assertEqual(await wrapped(4), 8)


class JobLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_registered_job_has_a_persian_label(self) -> None:
        from app.jobs import scheduler as scheduler_module
        from app.services.resource_cost import JOB_LABELS

        fresh = AsyncIOScheduler()
        with patch.object(scheduler_module, "scheduler", fresh):
            scheduler_module.start_scheduler(SimpleNamespace())
            ids = {job.id for job in fresh.get_jobs()}
            fresh.shutdown(wait=False)
        self.assertGreater(len(ids), 20)
        self.assertEqual(sorted(ids - set(JOB_LABELS)), [])
