"""Ask the background scheduler to run one of its jobs now, or to space one out.

The scheduler lives in ``app.jobs``, which services and routes must not import. It registers how to run a job
here when it starts; a route asks through ``run_job_now``. The job then runs exactly as on its timer: the
scheduler's one-instance rule means a run that is already going is not started a second time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_runner: Callable[[str], bool] | None = None


def register_job_runner(runner: Callable[[str], bool] | None) -> None:
    """Called by the scheduler when it starts (and with None when it stops)."""
    global _runner
    _runner = runner


def run_job_now(job_id: str) -> bool:
    """Bring the job's next run forward to now; False when the scheduler is not running or has no such job."""
    if _runner is None:
        return False
    return _runner(job_id)


_interval_setter: Callable[[str, int | None], bool] | None = None


def register_job_interval_setter(setter: Callable[[str, int | None], bool] | None) -> None:
    """Called by the scheduler when it starts (and with None when it stops)."""
    global _interval_setter
    _interval_setter = setter


def set_job_interval(job_id: str, seconds: int | None) -> bool:
    """Run the job every ``seconds`` (never more often than its own interval), or on its own interval again with
    None; False when the scheduler is not running or has no such job."""
    if _interval_setter is None:
        return False
    return _interval_setter(job_id, seconds)
