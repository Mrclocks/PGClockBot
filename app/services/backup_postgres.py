"""PostgreSQL dump and restore for full backups.

A restore runs in one transaction and stops at the first error, so it either
replaces the whole database or leaves it exactly as it was. Tool output is
logged on the server; callers only see the Persian message of
``BackupDatabaseError``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.config import get_settings
from app.db.engine_url import pg_connection_parts

log = logging.getLogger(__name__)

DUMP_TIMEOUT_SEC = 600
RESTORE_TIMEOUT_SEC = 900
CHECK_TIMEOUT_SEC = 120
_TOOL_DIRS = (Path("/usr/bin"), Path("/usr/local/bin"), Path("/bin"))

MISSING_TOOL_MESSAGE = "ابزار {tool} روی سرور نصب نیست؛ بستهٔ postgresql-client را نصب کنید."
DUMP_FAILED_MESSAGE = "ساخت نسخهٔ دیتابیس ناموفق بود؛ جزئیات در لاگ سرور ثبت شد."
VERSION_MESSAGE = (
    "نسخهٔ ابزار PostgreSQL این سرور با این بکاپ یا با سرور دیتابیس سازگار نیست؛ "
    "بستهٔ postgresql-client هم‌نسخهٔ سرور دیتابیس را نصب کنید."
)
UNREADABLE_MESSAGE = "فایل دیتابیس داخل بکاپ خوانده نشد؛ بکاپ ناقص یا آسیب‌دیده است."
BUSY_MESSAGE = (
    "دیتابیس مشغول بود و ریستور انجام نشد؛ هیچ تغییری اعمال نشد. چند دقیقه بعد دوباره تلاش کنید."
)
RESTORE_FAILED_MESSAGE = (
    "ریستور دیتابیس انجام نشد و دیتابیس بدون تغییر ماند؛ جزئیات در لاگ سرور ثبت شد."
)
TIMEOUT_MESSAGE = "عملیات دیتابیس بیش از حد طول کشید و متوقف شد؛ دیتابیس بدون تغییر ماند."


class BackupDatabaseError(RuntimeError):
    """A database backup or restore step failed; the message is safe to show to the admin."""


def find_tool(name: str) -> str | None:
    """Resolve a client tool even when systemd PATH is only ``.venv/bin``."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [directory / name for directory in _TOOL_DIRS]
    try:
        candidates += sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{name}"), reverse=True)
    except OSError:
        log.debug("listing /usr/lib/postgresql failed", exc_info=True)
    for path in candidates:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


def _require_tool(name: str) -> str:
    tool = find_tool(name)
    if not tool:
        raise BackupDatabaseError(MISSING_TOOL_MESSAGE.format(tool=name))
    return tool


def _connection() -> tuple[list[str], dict[str, str]]:
    """Command-line target and environment; the password never appears in argv."""
    parts = pg_connection_parts(get_settings().database_url)
    env = os.environ.copy()
    env.pop("PGPASSWORD", None)
    if parts.get("password"):
        env["PGPASSWORD"] = str(parts["password"])
    args = [
        "--host", str(parts["host"]),
        "--port", str(parts["port"]),
        "--username", str(parts["user"]),
        "--dbname", str(parts["dbname"]),
    ]
    return args, env


def _run(cmd: list[str], env: dict[str, str], *, timeout: int, step: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        log.warning("%s timed out after %ss", step, timeout)
        raise BackupDatabaseError(TIMEOUT_MESSAGE) from exc
    except OSError as exc:
        log.warning("%s could not start", step, exc_info=True)
        raise BackupDatabaseError(MISSING_TOOL_MESSAGE.format(tool=Path(cmd[0]).name)) from exc


def _failure_message(stderr: str, default: str) -> str:
    text = (stderr or "").lower()
    if "unsupported version" in text or "server version mismatch" in text or "aborting because of server version" in text:
        return VERSION_MESSAGE
    if "lock timeout" in text or "canceling statement due to lock timeout" in text:
        return BUSY_MESSAGE
    return default


def dump_database(dest: Path) -> int:
    """Write a custom-format dump of the configured database; returns its size in bytes."""
    pg_dump = _require_tool("pg_dump")
    target, env = _connection()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    cmd = [pg_dump, "--format=custom", "--no-owner", "--no-acl", "--file", str(dest), *target]
    proc = _run(cmd, env, timeout=DUMP_TIMEOUT_SEC, step="pg_dump")
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1:
        log.warning("pg_dump failed code=%s: %s", proc.returncode, (proc.stderr or proc.stdout or "")[:2000])
        raise BackupDatabaseError(_failure_message(proc.stderr, DUMP_FAILED_MESSAGE))
    return dest.stat().st_size


def check_dump_readable(dump_path: Path) -> None:
    """Read the dump's table of contents without touching the database."""
    pg_restore = _require_tool("pg_restore")
    proc = _run([pg_restore, "--list", str(dump_path)], os.environ.copy(), timeout=CHECK_TIMEOUT_SEC, step="pg_restore --list")
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        log.warning("pg_restore --list failed code=%s: %s", proc.returncode, (proc.stderr or "")[:2000])
        raise BackupDatabaseError(_failure_message(proc.stderr, UNREADABLE_MESSAGE))


def end_other_sessions() -> int:
    """Close the panel's other sessions on this database so the restore is not blocked.

    A dump resets ``lock_timeout`` to zero, so a session left idle in a
    transaction would otherwise make the restore wait forever. Only sessions of
    the panel's own role are closed (PostgreSQL allows nothing else without
    superuser); the service restarts right after a restore in any case.
    """
    from sqlalchemy import create_engine, text

    from app.db.engine_url import to_sync_url

    engine = create_engine(to_sync_url(get_settings().database_url), connect_args={"connect_timeout": 15})
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND usename = current_user "
                    "AND pid <> pg_backend_pid()"
                )
            ).all()
    except Exception as exc:  # noqa: BLE001 - driver errors vary; the restore must not start blind
        log.warning("could not close other database sessions before restore", exc_info=True)
        raise BackupDatabaseError(RESTORE_FAILED_MESSAGE) from exc
    finally:
        engine.dispose()
    return sum(1 for (closed,) in rows if closed)


def restore_database(dump_path: Path) -> None:
    """Replace the configured database with the dump, all or nothing."""
    pg_restore = _require_tool("pg_restore")
    target, env = _connection()
    closed = end_other_sessions()
    if closed:
        log.info("closed %s panel database sessions before restore", closed)
    cmd = [
        pg_restore,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--single-transaction",
        "--exit-on-error",
        *target,
        str(dump_path),
    ]
    proc = _run(cmd, env, timeout=RESTORE_TIMEOUT_SEC, step="pg_restore")
    if proc.returncode != 0:
        log.warning("pg_restore failed code=%s: %s", proc.returncode, (proc.stderr or proc.stdout or "")[:2000])
        raise BackupDatabaseError(_failure_message(proc.stderr, RESTORE_FAILED_MESSAGE))
