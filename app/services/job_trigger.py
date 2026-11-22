"""Ask the background scheduler to run one of its jobs now.

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
