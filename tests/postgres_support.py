"""Isolated PostgreSQL databases for tests.

Tests run against a real PostgreSQL server named by ``TEST_DATABASE_URL``.
Every test gets its own throwaway database, so tests stay independent, can
commit, can open concurrent sessions and can run in parallel under xdist.
A database with the app tables is cloned from a template built once per
schema version (``CREATE DATABASE ... TEMPLATE``), which is several times
faster than creating the tables for every test.

* ``PostgresTestCase``: base class with ``self.engine`` and ``self.Session``.
* ``isolated_database``: async context manager for one database.
* ``scoped_database_url`` / ``scoped_async_engine`` / ``scoped_sync_engine``:
  plain functions for tests that build their own engine. Repeated calls in
  one test return the same database (the way one database file used to be
  shared); ``tests/conftest.py`` drops it when the test ends.

A missing ``TEST_DATABASE_URL`` is an error, not a skip, so a test can never
pass silently without a database behind it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import Base

from app.db.models import BotUser

logger = logging.getLogger(__name__)

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
DATABASE_PREFIX = "pgct_db_"
TEMPLATE_PREFIX = "pgct_tmpl_"
_NAME_TOKEN_BYTES = 8
_TEMPLATE_HASH_CHARS = 16
# Bump when _build_template changes what goes into the template besides the models.
_TEMPLATE_REVISION = 2
# Keeps add_shop_users' Telegram ids clear of the ones tests choose.
SHOP_TELEGRAM_ID_BASE = 9_000_000_000
# First value of every id sequence in a cloned database.
GENERATED_ID_START = 1_000_001
_DROP_ATTEMPTS = 20
_SESSION_DROP_ATTEMPTS = 100
_DROP_RETRY_SECONDS = 0.1
# Serializes template creation across xdist workers sharing one server.
_TEMPLATE_LOCK_KEY = 0x50474354

MISSING_URL_MESSAGE = (
    f"{TEST_DATABASE_URL_ENV} is not set. Tests need a PostgreSQL server, for example:\n"
    f"  export {TEST_DATABASE_URL_ENV}=postgresql+asyncpg://user:password@127.0.0.1:5432/pgclock_test\n"
    "Use a dedicated server and a superuser role, as CI does: every test creates its own\n"
    "database, and a few migration tests edit system catalogs."
)


class MissingTestDatabaseError(RuntimeError):
    """Raised when a database test runs without TEST_DATABASE_URL."""


@dataclass(slots=True)
class IsolatedDatabase:
    """A throwaway database and the handles a test needs."""

    url: str
    engine: AsyncEngine
    Session: async_sessionmaker[AsyncSession]


@dataclass(slots=True)
class DatabaseScope:
    """Databases created for one test (or for the session, outside any test)."""

    databases: dict[tuple[str, bool], str] = field(default_factory=dict)


def postgres_test_url() -> str:
    """Return the asyncpg URL of the test server, or fail loudly."""
    raw = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not raw:
        raise MissingTestDatabaseError(MISSING_URL_MESSAGE)
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise MissingTestDatabaseError(f"{TEST_DATABASE_URL_ENV} must point at PostgreSQL")
    return _render(url.set(drivername="postgresql+asyncpg"))


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _sync_url(async_url: str) -> str:
    return _render(make_url(async_url).set(drivername="postgresql+psycopg"))


def _url_for(database: str) -> str:
    return _render(make_url(postgres_test_url()).set(database=database))


def _quoted(identifier: str) -> str:
    # Names come only from this module (prefix + hex), never from test input.
    if not identifier.startswith((DATABASE_PREFIX, TEMPLATE_PREFIX)) or not identifier.replace("_", "").isalnum():
        raise ValueError("refusing to quote an identifier that was not generated here")
    return f'"{identifier}"'


def _admin_engine() -> Engine:
    return create_engine(_sync_url(postgres_test_url()), poolclass=NullPool, isolation_level="AUTOCOMMIT")


def _schema_fingerprint() -> str:
    dialect = postgresql.dialect()
    tables = sorted(Base.metadata.tables.values(), key=lambda table: table.name)
    statements = [str(CreateTable(table).compile(dialect=dialect)) for table in tables]
    statements += [
        str(CreateIndex(index).compile(dialect=dialect))
        for table in tables
        for index in sorted(table.indexes, key=lambda item: item.name or "")
    ]
    statements.append(f"template-revision {_TEMPLATE_REVISION}")
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()[:_TEMPLATE_HASH_CHARS]


_template_name: str | None = None


def _ensure_template() -> str:
    """Return the template database holding the current tables, building it once."""
    global _template_name
    if _template_name is not None:
        return _template_name
    name = TEMPLATE_PREFIX + _schema_fingerprint()
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _TEMPLATE_LOCK_KEY})
            try:
                exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name})
                if not exists:
                    _build_template(conn, name)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _TEMPLATE_LOCK_KEY})
    finally:
        admin.dispose()
    _template_name = name
    return name


def _build_template(admin_conn: Any, name: str) -> None:
    building = name + "_" + secrets.token_hex(4)
    admin_conn.execute(text(f"CREATE DATABASE {_quoted(building)}"))
    engine = create_engine(_sync_url(_url_for(building)), poolclass=NullPool)
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
            _start_sequences_high(conn)
    finally:
        engine.dispose()
    # Renamed only once complete, so a crash never leaves a half-built template behind.
    admin_conn.execute(text(f"ALTER DATABASE {_quoted(building)} RENAME TO {_quoted(name)}"))
    _drop_stale_templates(admin_conn, keep=name)


def _drop_stale_templates(admin_conn: Any, *, keep: str) -> None:
    """Remove templates built for older models; one still being cloned is left alone."""
    stale = admin_conn.scalars(
        text("SELECT datname FROM pg_database WHERE datname LIKE :prefix AND datname <> :keep"),
        {"prefix": TEMPLATE_PREFIX + "%", "keep": keep},
    ).all()
    for old in stale:
        try:
            admin_conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(old)}"))
        except DBAPIError:
            logger.warning("could not drop stale test template %s", old, exc_info=True)


def _start_sequences_high(conn: Any) -> None:
    """Keep generated ids clear of the small explicit ids tests seed (id=1, id=2, ...)."""
    sequences = conn.scalars(
        text("SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public'")
    ).all()
    for sequence in sequences:
        conn.execute(
            text("SELECT setval(CAST(:sequence AS regclass), :start, false)"),
            {"sequence": f'"{sequence}"', "start": GENERATED_ID_START},
        )


def create_database(*, tables: bool) -> str:
    """Create a fresh database (with or without the app tables) and return its asyncpg URL."""
    name = DATABASE_PREFIX + secrets.token_hex(_NAME_TOKEN_BYTES)
    template = f" TEMPLATE {_quoted(_ensure_template())}" if tables else ""
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {_quoted(name)}{template}"))
    finally:
        admin.dispose()
    return _url_for(name)


def drop_database(url: str, *, attempts: int = _DROP_ATTEMPTS) -> bool:
    """Drop a database made by create_database, closing any connection a test left open.

    FORCE cannot end an autovacuum worker, which runs as a superuser, when the
    test role is not one; such a worker finishes quickly, so retry for a while.
    Returns False if the database is still there.
    """
    name = make_url(url).database or ""
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            for attempt in range(attempts):
                try:
                    conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(name)} WITH (FORCE)"))
                    return True
                except DBAPIError:
                    if attempt == attempts - 1:
                        logger.warning("could not drop test database %s", name, exc_info=True)
                        return False
                    time.sleep(_DROP_RETRY_SECONDS)
    finally:
        admin.dispose()
    return False


# --- per-test scope, driven by tests/conftest.py ---------------------------

_session_scope = DatabaseScope()
_active_scope: DatabaseScope | None = None
# Databases an autovacuum worker kept busy at test end; retried once more at session end.
_undropped: list[str] = []


def begin_scope() -> DatabaseScope:
    global _active_scope
    _active_scope = DatabaseScope()
    return _active_scope


def end_scope(scope: DatabaseScope) -> None:
    global _active_scope
    if _active_scope is scope:
        _active_scope = None
    _drop_scope(scope)


def end_session() -> None:
    _drop_scope(_session_scope)
    for url in list(_undropped):
        if drop_database(url, attempts=_SESSION_DROP_ATTEMPTS):
            _undropped.remove(url)


def _drop_scope(scope: DatabaseScope) -> None:
    for url in scope.databases.values():
        if not drop_database(url):
            _undropped.append(url)
    scope.databases.clear()


def scoped_database_url(*, tables: bool = True, key: str = "") -> str:
    """Return the current test's database URL, creating the database on first use.

    Calls with the same ``tables`` and ``key`` share one database; pass a
    different ``key`` when a test needs a second, separate database.
    """
    scope = _active_scope or _session_scope
    slot = (key, tables)
    if slot not in scope.databases:
        scope.databases[slot] = create_database(tables=tables)
    return scope.databases[slot]


def scoped_async_engine(*, tables: bool = True, key: str = "", **kwargs: Any) -> AsyncEngine:
    """An async engine on the current test's database (app tables included by default)."""
    return create_async_engine(scoped_database_url(tables=tables, key=key), **kwargs)


def scoped_sync_engine(*, tables: bool = False, key: str = "", **kwargs: Any) -> Engine:
    """A sync engine on the current test's database; empty by default, for migration tests."""
    return create_engine(_sync_url(scoped_database_url(tables=tables, key=key)), **kwargs)


async def add_shop_users(sessions: async_sessionmaker[AsyncSession], *shop_ids: int) -> None:
    """Insert the reseller users that shop-scoped test rows point at.

    PostgreSQL enforces foreign keys, so a row with ``reseller_id=41`` needs user 41.
    Telegram ids and referral codes are kept far from the small values tests use.
    """
    async with sessions() as session:
        session.add_all(
            BotUser(
                id=shop_id,
                telegram_id=SHOP_TELEGRAM_ID_BASE + shop_id,
                referral_code=f"test-shop-{shop_id}",
                role="reseller",
            )
            for shop_id in shop_ids
        )
        await session.commit()


@asynccontextmanager
async def isolated_database(*, create_tables: bool = False) -> AsyncIterator[IsolatedDatabase]:
    """Yield a fresh database (tables only on request); drop it afterwards."""
    url = await asyncio.to_thread(create_database, tables=create_tables)
    engine = create_async_engine(url)
    try:
        yield IsolatedDatabase(url=url, engine=engine, Session=async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
        if not await asyncio.to_thread(drop_database, url):
            _undropped.append(url)


class PostgresTestCase(unittest.IsolatedAsyncioTestCase):
    """Base class giving every test ``self.engine`` and ``self.Session`` in its own database."""

    create_tables: ClassVar[bool] = True

    engine: AsyncEngine
    Session: async_sessionmaker[AsyncSession]
    database: IsolatedDatabase

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        context = isolated_database(create_tables=self.create_tables)
        self.database = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.engine = self.database.engine
        self.Session = self.database.Session
