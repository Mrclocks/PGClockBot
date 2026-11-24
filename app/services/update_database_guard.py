"""Refuse panel updates on a server whose database is not PostgreSQL.

The panel runs on PostgreSQL only. An update on a server whose .env points at
any other database would replace working code with code that cannot start, so
both the web panel and ``pgclock.sh update`` stop before touching anything.
"""

from __future__ import annotations

from typing import Any

from app.config import ROOT_DIR, get_settings
from app.services.backup_env import env_value

# Same prefixes the installer and pgclock.sh accept.
POSTGRES_URL_PREFIXES = ("postgresql://", "postgresql+asyncpg://", "postgres://")
UNSUPPORTED_DATABASE_LABEL = "دیتابیس شما پشتیبانی نمی‌شود"
UNSUPPORTED_DATABASE_MESSAGE = (
    "دیتابیس شما پشتیبانی نمی‌شود: PGClockBot فقط با PostgreSQL کار می‌کند. "
    "تا وقتی DATABASE_URL در فایل .env به PostgreSQL اشاره نکند، آپدیت انجام نمی‌شود. "
    "برای ساختن دیتابیس PostgreSQL، Install را از منوی pgclock.sh اجرا کنید."
)


def installed_database_url() -> str:
    """The DATABASE_URL the updated panel would start with: the one in .env."""
    env_path = ROOT_DIR / ".env"
    if env_path.is_file():
        return (env_value(env_path.read_text(encoding="utf-8"), "DATABASE_URL") or "").strip()
    return (get_settings().database_url or "").strip()


def is_supported_database_url(url: str) -> bool:
    return url.strip().startswith(POSTGRES_URL_PREFIXES)


def database_update_block() -> dict[str, Any] | None:
    """A blocked update preflight when the database is not PostgreSQL, else None."""
    if is_supported_database_url(installed_database_url()):
        return None
    return {
        "checked": True,
        "blocked": True,
        "db_revision": None,
        "tone": "err",
        "label": UNSUPPORTED_DATABASE_LABEL,
        "message": UNSUPPORTED_DATABASE_MESSAGE,
    }
