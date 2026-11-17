#!/usr/bin/env python3
"""Put the previous code back when an in-panel update never starts.

Runs as systemd ``ExecStartPre`` before every start of the panel service. The web
update writes ``data/update_pending.json`` right before it restarts; the new
version deletes it once its startup finished. When the marker is still there
after ``MAX_FAILED_BOOTS`` starts, the new version is crashing: if the database
schema is still the pre-update one, the previous commit is checked out so the next
start runs the old version. It never fails the unit (``ExecStartPre=-``) and uses
only the standard library apart from an optional schema read, because the code it
guards may be broken.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER_NAME = "update_pending.json"
STATUS_NAME = "panel_update.json"
MAX_FAILED_BOOTS = 3
GIT_TIMEOUT_SEC = 60
RUNTIME_CLEAN_EXCLUDES = (".env", "data", ".venv", ".env.bak.*", ".env.restored.*")

logger = logging.getLogger("pgclock.update_boot_guard")

ROLLED_BACK = (
    "نسخهٔ جدید بعد از آپدیت بالا نیامد؛ نسخهٔ قبلی به‌طور خودکار برگردانده شد. "
    "گزارش خطا: journalctl -u pgclockbot -n 200"
)
SCHEMA_MOVED = (
    "نسخهٔ جدید بعد از آپدیت بالا نیامد و ساختار دیتابیس تغییر کرده است. "
    "پشتیبان {backup} را با pgclock restore بازگردانید و سپس آپدیت را دوباره امتحان کنید."
)


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "").strip()


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _report(root: Path, message: str) -> None:
    """Show the outcome on the update page (same file the web update writes)."""
    status_path = root / "data" / STATUS_NAME
    status = _read_json(status_path) or {}
    status.update(
        {
            "state": "error",
            "message": message,
            "error": message,
            "awaiting_restart": False,
            "restart_required": False,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_json(status_path, status)


def read_schema_revision(root: Path) -> str | None:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from app.config import get_settings
        from app.db.alembic_runner import current_revision

        return current_revision(get_settings().database_url)
    except Exception:  # noqa: BLE001 - broken new code or DB: the schema is simply unknown
        logger.warning("boot guard could not read the schema revision", exc_info=True)
        return None


def restore_previous(root: Path, previous: dict) -> bool:
    commit = str(previous.get("commit") or "")
    branch = str(previous.get("branch") or "")
    if not commit:
        return False
    checkout = ["checkout", "-f", "-q", "-B", branch, commit] if branch else ["checkout", "-f", "-q", "--detach", commit]
    for args in (checkout, ["reset", "--hard", "-q", commit]):
        code, out = _git(root, *args)
        if code != 0:
            logger.error("boot guard: git %s failed: %s", args[0], out[-300:])
            return False
    _git(root, "clean", "-fd", *(f"--exclude={item}" for item in RUNTIME_CLEAN_EXCLUDES))
    return True


def run(root: Path, *, schema_reader: Callable[[Path], str | None] = read_schema_revision) -> str:
    """One guard pass; returns what it did (for logs and tests)."""
    marker = root / "data" / MARKER_NAME
    if not marker.is_file():
        return "no-pending-update"
    pending = _read_json(marker)
    _, head = _git(root, "rev-parse", "HEAD")
    if pending is None or head != pending.get("new_commit"):
        # Unreadable, or the code was changed by something else since: nothing to guard.
        marker.unlink(missing_ok=True)
        return "stale-marker"
    attempts = int(pending.get("attempts") or 0) + 1
    if attempts <= MAX_FAILED_BOOTS:
        pending["attempts"] = attempts
        _write_json(marker, pending)
        return f"attempt-{attempts}"
    previous = pending.get("previous") if isinstance(pending.get("previous"), dict) else {}
    marker.unlink(missing_ok=True)
    schema_before = previous.get("schema")
    if not schema_before or schema_reader(root) != schema_before:
        _report(root, SCHEMA_MOVED.format(backup=pending.get("backup_id") or "قبل از آپدیت"))
        return "schema-moved"
    if not restore_previous(root, previous):
        _report(root, "نسخهٔ جدید بالا نیامد و برگرداندن نسخهٔ قبلی ناموفق بود. روی سرور از هر پوشه: pgclock update")
        return "restore-failed"
    _report(root, ROLLED_BACK)
    return "rolled-back"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        outcome = run(ROOT)
    except Exception:  # noqa: BLE001 - the guard must never keep the panel from starting
        logger.exception("update boot guard failed")
        return 0
    if outcome not in ("no-pending-update",):
        logger.info("update boot guard: %s", outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
