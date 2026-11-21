"""In-process run statistics for scheduled background jobs.

The scheduler reports when a job starts, ends, fails or is skipped because the
previous run is still going. These numbers show which background work costs the
most on a server, and feed the resource view in settings. They live in memory
only: a restart starts counting again, which is enough to compare jobs on one
running process.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ParamSpec, TypeVar

# Job ids come from code, but a bound keeps a bug from growing the table without limit.
MAX_TRACKED_JOBS = 128

P = ParamSpec("P")
R = TypeVar("R")

# The scheduled job whose code is running in this task, for the error-log handler below.
_current_job: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_job", default=None)


@dataclass(slots=True)
class JobStats:
    job_id: str
    runs: int = 0
    failures: int = 0
    overlaps: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0
    last_seconds: float = 0.0
    last_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None

    @property
    def average_seconds(self) -> float:
        return self.total_seconds / self.runs if self.runs else 0.0


@dataclass(frozen=True, slots=True)
class JobStatsView:
    job_id: str
    runs: int
    failures: int
    overlaps: int
    average_seconds: float
    max_seconds: float
    last_seconds: float
    total_seconds: float
    last_finished_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None


class JobMetrics:
    """Thread-safe registry; scheduler listeners may run outside the event loop thread."""

    def __init__(self, *, max_jobs: int = MAX_TRACKED_JOBS) -> None:
        self._max_jobs = max_jobs
        self._stats: dict[str, JobStats] = {}
        self._started: dict[str, float] = {}
        # Jobs whose current run logged an error: jobs catch their own failures and log them.
        self._failed_runs: set[str] = set()
        self._lock = threading.Lock()

    def _entry(self, job_id: str) -> JobStats | None:
        entry = self._stats.get(job_id)
        if entry is None and len(self._stats) < self._max_jobs:
            entry = self._stats[job_id] = JobStats(job_id)
        return entry

    def mark_started(self, job_id: str, *, now: float | None = None) -> None:
        with self._lock:
            self._failed_runs.discard(job_id)
            if job_id in self._stats or len(self._stats) < self._max_jobs:
                self._started[job_id] = time.monotonic() if now is None else now

    def mark_finished(
        self, job_id: str, *, succeeded: bool, now: float | None = None, finished_at: datetime | None = None
    ) -> None:
        moment = time.monotonic() if now is None else now
        finished = finished_at or datetime.now(timezone.utc)
        with self._lock:
            started = self._started.pop(job_id, None)
            if job_id in self._failed_runs:
                self._failed_runs.discard(job_id)
                succeeded = False
            entry = self._entry(job_id)
            if entry is None:
                return
            duration = max(0.0, moment - started) if started is not None else 0.0
            entry.runs += 1
            entry.total_seconds += duration
            entry.last_seconds = duration
            entry.max_seconds = max(entry.max_seconds, duration)
            entry.last_finished_at = finished
            if succeeded:
                entry.last_success_at = finished
            else:
                entry.failures += 1
                entry.last_failure_at = finished

    def note_failure(self, job_id: str) -> None:
        """The running job logged an error it handled itself; its run counts as failed."""
        with self._lock:
            if job_id in self._started:
                self._failed_runs.add(job_id)

    def mark_overlap(self, job_id: str) -> None:
        """The job was due again while its previous run was still going."""
        with self._lock:
            entry = self._entry(job_id)
            if entry is not None:
                entry.overlaps += 1

    def snapshot(self) -> list[JobStatsView]:
        """Jobs ordered by total busy time, the heaviest first."""
        with self._lock:
            views = [
                JobStatsView(
                    job_id=s.job_id,
                    runs=s.runs,
                    failures=s.failures,
                    overlaps=s.overlaps,
                    average_seconds=s.average_seconds,
                    max_seconds=s.max_seconds,
                    last_seconds=s.last_seconds,
                    total_seconds=s.total_seconds,
                    last_finished_at=s.last_finished_at,
                    last_success_at=s.last_success_at,
                    last_failure_at=s.last_failure_at,
                )
                for s in self._stats.values()
            ]
        return sorted(views, key=lambda view: view.total_seconds, reverse=True)

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()
            self._started.clear()
            self._failed_runs.clear()


job_metrics = JobMetrics()


def tracked_job(job_id: str, func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """``func`` run with ``job_id`` as the current job, so errors it logs count against that job."""

    @functools.wraps(func)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        token = _current_job.set(job_id)
        try:
            return await func(*args, **kwargs)
        finally:
            _current_job.reset(token)

    return run


class JobErrorLogHandler(logging.Handler):
    """Marks the running job's run as failed when anything logs at ERROR or above during it."""

    def __init__(self, metrics: JobMetrics | None = None) -> None:
        super().__init__(level=logging.ERROR)
        self._metrics = metrics

    def emit(self, record: logging.LogRecord) -> None:
        job_id = _current_job.get()
        if job_id is not None:
            (self._metrics or job_metrics).note_failure(job_id)


_handler_lock = threading.Lock()
_installed: list[JobErrorLogHandler] = []


def install_job_error_handler(logger: logging.Logger | None = None) -> None:
    """Attach the handler once to the root logger (or ``logger``); repeated calls do nothing."""
    target = logger or logging.getLogger()
    with _handler_lock:
        if any(handler in target.handlers for handler in _installed):
            return
        handler = JobErrorLogHandler()
        target.addHandler(handler)
        _installed.append(handler)
