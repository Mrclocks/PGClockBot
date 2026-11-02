"""Bring back a backed-up ``.env`` without breaking this server's own setup.

A full backup moved to a new server carries the old server's database
password, listen address and installed tool paths. Those stay as this
server has them; everything else (bot token, WEB_SECRET, PasarGuard access,
public URLs) comes from the backup. After a restore the stored secrets are
checked against the WEB_SECRET that will be in effect, so the admin learns at
once when they cannot be read.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from app.services.secret_box import decrypts_with_secret

log = logging.getLogger(__name__)

# Set by the installer for this machine; a backup from another server must not replace them.
SERVER_LOCAL_KEYS = (
    "DATABASE_URL",
    "WEB_HOST",
    "WEB_PORT",
    "CONNECTION_MONITOR_XRAY_PATH",
    "CONNECTION_MONITOR_XRAY_SHA256",
    "DEPLOYED_CHANNEL",
)
# Columns encrypted with WEB_SECRET (fixed allow-list: the names go into SQL).
ENCRYPTED_COLUMNS = (
    ("reseller_profiles", "pg_admin_password_enc"),
    ("reseller_profiles", "pg_api_key_enc"),
    ("pg_staff_access", "pg_admin_password_enc"),
    ("pg_staff_access", "pg_api_key_enc"),
    ("org_principals", "pg_password_enc"),
    ("org_principals", "pg_api_key_enc"),
)
SAMPLE_PER_COLUMN = 5
UNREADABLE_SECRETS_WARNING = (
    "رمزها و کلیدهای پاسارگارد ذخیره‌شده با کلید این سرور (WEB_SECRET) باز نمی‌شوند. "
    "اگر بکاپ از سرور دیگری است، همان بکاپ را با گزینهٔ «بازیابی .env» دوباره ریستور کنید؛ "
    "وگرنه رمز یا کلید API نماینده‌ها را دوباره ثبت کنید."
)

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _assignments(text: str) -> dict[str, str]:
    """Last assignment line of every key (dotenv semantics)."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match:
            found[match.group(1)] = line
    return found


def env_value(text: str, key: str) -> str | None:
    line = _assignments(text).get(key)
    if line is None:
        return None
    match = _ASSIGNMENT.match(line)
    value = match.group(2).strip() if match else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def merge_restored_env(backup_text: str, current_text: str) -> str:
    """The backup's ``.env`` with this server's own keys kept as they are now."""
    current = _assignments(current_text)
    lines: list[str] = []
    seen: set[str] = set()
    for line in backup_text.splitlines():
        match = _ASSIGNMENT.match(line)
        key = match.group(1) if match else None
        if key in SERVER_LOCAL_KEYS:
            seen.add(key)
            if key in current:
                lines.append(current[key])
                continue
        lines.append(line)
    missing = [current[key] for key in SERVER_LOCAL_KEYS if key in current and key not in seen]
    if missing:
        lines += ["", "# Kept from this server during backup restore", *missing]
    return "\n".join(lines) + "\n"


def write_private_file(path: Path, text: str) -> None:
    """Never leave a half-written file: write a private temp file, then rename it."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def stored_secrets_readable(database_url: str, web_secret: str) -> bool | None:
    """True if stored secrets decrypt with ``web_secret``, False if none do, None if there are none."""
    from sqlalchemy import create_engine, inspect, text

    from app.db.engine_url import to_sync_url

    samples: list[str] = []
    url = to_sync_url(database_url)
    engine = create_engine(url, connect_args={"connect_timeout": 15} if url.startswith("postgresql") else {})
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            for table, column in ENCRYPTED_COLUMNS:
                if table not in tables:
                    continue
                rows = conn.execute(
                    text(f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" <> \'\' LIMIT :n'),
                    {"n": SAMPLE_PER_COLUMN},
                )
                samples += [str(value) for (value,) in rows]
    finally:
        engine.dispose()
    if not samples:
        return None
    return any(decrypts_with_secret(sample, web_secret) for sample in samples)
