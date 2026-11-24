"""Database URL helpers — engine detection and dialect conversion."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, unquote


@dataclass(frozen=True)
class EngineInfo:
    dialect: str  # always "postgresql"
    url: str
    pg_dsn: str | None = None  # libpq-style for pg_dump (sync)


def normalize_async_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError("DATABASE_URL is empty")
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://") :]
    if u.startswith("postgresql://") and "+asyncpg" not in u.split("://", 1)[0]:
        u = "postgresql+asyncpg://" + u.split("://", 1)[1]
    return u


def to_sync_url(url: str) -> str:
    """Convert async SQLAlchemy URL to a sync driver URL (Alembic / pg tools)."""
    u = normalize_async_url(url)
    if u.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + u[len("postgresql+asyncpg://") :]
    return u


def parse_engine(url: str) -> EngineInfo:
    u = normalize_async_url(url)
    if not u.startswith("postgresql"):
        raise ValueError(f"DATABASE_URL must be PostgreSQL, got {u.split(':', 1)[0]}")
    return EngineInfo(dialect="postgresql", url=u, pg_dsn=_asyncpg_url_to_libpq(u))


def _asyncpg_url_to_libpq(url: str) -> str:
    """Build a libpq connection URI for pg_dump/pg_restore."""
    sync = to_sync_url(url)
    # postgresql+psycopg://user:pass@host:port/db → postgresql://...
    if sync.startswith("postgresql+psycopg://"):
        return "postgresql://" + sync[len("postgresql+psycopg://") :]
    if sync.startswith("postgresql+psycopg2://"):
        return "postgresql://" + sync[len("postgresql+psycopg2://") :]
    return sync


def pg_connection_parts(url: str) -> dict[str, str | int | None]:
    dsn = parse_engine(url).pg_dsn or ""
    parsed = urlparse(dsn)
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or "") if parsed.password is not None else None,
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": (parsed.path or "/").lstrip("/") or "pgclock",
    }
