"""Safety steps around the in-panel update: backup, checks, rollback, boot marker.

The web panel updates itself while it is still serving. Everything that can fail
(packages, imports, migrations) is tried while the old process keeps running, so a
failure puts the previous code back instead of leaving a panel that cannot start.
The one step that cannot be tried in advance is the restart itself; for that a
boot marker lets ``scripts/update_boot_guard.py`` (systemd ``ExecStartPre``)
restore the previous code when the new version keeps failing to start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.config import DATA_DIR
from app.services.redact import redact

logger = logging.getLogger(__name__)

BOOT_MARKER = DATA_DIR / "update_pending.json"
REQUIREMENTS_HASH = Path(".venv") / ".requirements.sha256"
RUNTIME_CLEAN_EXCLUDES = (".env", "data", ".venv", ".env.bak.*", ".env.restored.*")
PIP_TIMEOUT_SEC = 900
IMPORT_CHECK_TIMEOUT_SEC = 180
MIGRATE_TIMEOUT_SEC = 300
GIT_TIMEOUT_SEC = 120

# Imports every entry point the service loads, without starting anything.
IMPORT_CHECK_CODE = "import app.main, app.bot, app.api.app, app.jobs.scheduler"
# Same schema step the service runs on boot (``init_db``), in a separate process.
MIGRATE_CODE = (
    "import asyncio\n"
    "from app.db.session import init_db\n"
    "asyncio.run(asyncio.wait_for(init_db(), timeout=240))\n"
)


class UpdateRunner(Protocol):
    def __call__(self, command: list[str], *, cwd: Path, timeout: int) -> tuple[int, str]: ...


class UpdateStepError(RuntimeError):
    """A pre-restart step failed; the message is safe to show to the owner."""


@dataclass(slots=True)
class RollbackPoint:
    commit: str
    branch: str
    schema: str | None


def _tail(output: str, limit: int = 300) -> str:
    """Last meaningful line of tool output, with secrets (DB URLs, tokens) redacted."""
    lines = [line for line in (output or "").strip().splitlines() if line.strip()]
    return redact(lines[-1] if lines else "", limit=limit)


def read_schema_revision() -> str | None:
    """Alembic revision of the live database, ``None`` when it cannot be read."""
    from app.config import get_settings  # deferred: settings are read at call time
    from app.db.alembic_runner import current_revision

    try:
        return current_revision(get_settings().database_url)
    except Exception:  # noqa: BLE001 - an unreadable revision only disables rollback
        logger.warning("could not read the schema revision before update", exc_info=True)
        return None


def capture_rollback_point(git: str, root: Path, *, runner: UpdateRunner) -> RollbackPoint:
    code, commit = runner([git, "rev-parse", "--verify", "HEAD"], cwd=root, timeout=30)
    if code != 0 or not commit.strip():
        raise UpdateStepError("نسخهٔ فعلی کد مشخص نیست؛ آپدیت انجام نشد")
    _, branch = runner([git, "symbolic-ref", "-q", "--short", "HEAD"], cwd=root, timeout=30)
    return RollbackPoint(
        commit=commit.strip().splitlines()[-1],
        branch=branch.strip() if branch.strip() and " " not in branch.strip() else "",
        schema=read_schema_revision(),
    )


def backup_database(*, log: Callable[[str], None]) -> str:
    """Full backup before anything changes; a failure stops the update."""
    from app.services.backup import create_backup  # deferred: pulls in the DB layer

    try:
        meta = create_backup(note="pre-update", include_env=True, created_by="panel-update")
    except Exception as exc:
        logger.exception("pre-update backup failed")
        raise UpdateStepError(
            f"پشتیبان‌گیری قبل از آپدیت ناموفق بود ({_tail(str(exc), 200)})؛ چیزی تغییر نکرد. "
            "اگر pg_dump نصب نیست، روی سرور bash pgclock.sh و گزینهٔ Update را بزنید "
            "(ابزار پشتیبان را نصب می‌کند و همان آپدیت امن را انجام می‌دهد)."
        ) from exc
    backup_id = str(meta.get("id") or meta.get("backup_id") or "")
    log(f"پشتیبان کامل ساخته شد: {backup_id or 'data/backups'}")
    return backup_id


def _requirements_digest(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def install_requirements(
    root: Path, requirements: bytes, *, python: str, runner: UpdateRunner, log: Callable[[str], None]
) -> None:
    """Install a requirements file's content into the venv, skipping unchanged content."""
    digest = _requirements_digest(requirements)
    hash_file = root / REQUIREMENTS_HASH
    if hash_file.is_file() and hash_file.read_text(encoding="utf-8").strip() == digest:
        log("وابستگی‌ها تغییری نکرده‌اند")
        return
    with tempfile.NamedTemporaryFile("wb", suffix=".txt", prefix="pgclock-req-", delete=False) as tmp:
        tmp.write(requirements)
        req_path = Path(tmp.name)
    try:
        cmd = [python, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-input", "-r", str(req_path)]
        code, out = runner(cmd, cwd=root, timeout=PIP_TIMEOUT_SEC)
    finally:
        req_path.unlink(missing_ok=True)
    if code != 0:
        logger.warning("pip install failed during panel update: %s", _tail(out, 2000))
        raise UpdateStepError(f"نصب پکیج‌های پایتون ناموفق بود: {_tail(out)}")
    hash_file.parent.mkdir(parents=True, exist_ok=True)
    hash_file.write_text(digest + "\n", encoding="utf-8")
    log("وابستگی‌ها نصب شد")


def requirements_at(git: str, root: Path, ref: str, *, runner: UpdateRunner) -> bytes | None:
    """``requirements.txt`` of ``ref`` without checking it out."""
    code, out = runner([git, "show", f"{ref}:requirements.txt"], cwd=root, timeout=30)
    if code != 0:
        return None
    return (out.rstrip("\n") + "\n").encode("utf-8")


def switch_code(
    git: str, root: Path, *, commit: str, branch: str, runner: UpdateRunner, start: str | None = None
) -> None:
    """Hard-sync the work tree to ``commit``; runtime files are kept.

    ``start`` (``origin/<branch>``) keeps the branch's upstream; ``commit`` pins the
    exact revision that was checked, even if the remote moved meanwhile.
    """
    checkout = [git, "checkout", "-f", "-q"]
    checkout += ["-B", branch, start or commit] if branch else ["--detach", commit]
    for cmd in (checkout, [git, "reset", "--hard", "-q", commit]):
        code, out = runner(cmd, cwd=root, timeout=GIT_TIMEOUT_SEC)
        if code != 0:
            raise UpdateStepError(f"تغییر کد ناموفق بود: {_tail(out)}")
    excludes = [f"--exclude={item}" for item in RUNTIME_CLEAN_EXCLUDES]
    runner([git, "clean", "-fd", *excludes], cwd=root, timeout=60)


def verify_new_code(root: Path, *, python: str, runner: UpdateRunner) -> None:
    """Import the new code in a fresh process: catches syntax and missing packages."""
    code, out = runner([python, "-c", IMPORT_CHECK_CODE], cwd=root, timeout=IMPORT_CHECK_TIMEOUT_SEC)
    if code != 0:
        logger.warning("new code failed the import check: %s", _tail(out, 2000))
        raise UpdateStepError(f"نسخهٔ جدید بارگذاری نشد: {_tail(out)}")


def migrate_schema(root: Path, *, python: str, runner: UpdateRunner) -> None:
    code, out = runner([python, "-c", MIGRATE_CODE], cwd=root, timeout=MIGRATE_TIMEOUT_SEC)
    if code != 0:
        logger.warning("schema migration failed during panel update: %s", _tail(out, 2000))
        raise UpdateStepError(f"به‌روزرسانی ساختار دیتابیس ناموفق بود: {_tail(out)}")


def python_for(root: Path) -> str:
    venv_py = root / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def write_boot_marker(point: RollbackPoint, *, new_commit: str, backup_id: str) -> None:
    """Tell the boot guard which version to restore if the new one never starts."""
    payload = {
        "previous": asdict(point),
        "new_commit": new_commit,
        "backup_id": backup_id,
        "attempts": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    BOOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOT_MARKER.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BOOT_MARKER)


def confirm_boot() -> None:
    """The service started: the pending update is confirmed and the marker goes."""
    try:
        BOOT_MARKER.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove the update boot marker", exc_info=True)
