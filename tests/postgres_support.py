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

# Importing the models registers every table on Base.metadata.
import app.db.models  # noqa: F401

logger = logging.getLogger(__name__)

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
DATABASE_PREFIX = "pgct_db_"
TEMPLATE_PREFIX = "pgct_tmpl_"
_NAME_TOKEN_BYTES = 8
_TEMPLATE_HASH_CHARS = 16
# Serializes template creation across xdist workers sharing one server.
_TEMPLATE_LOCK_KEY = 0x50474354

MISSING_URL_MESSAGE = (
    f"{TEST_DATABASE_URL_ENV} is not set. Tests need a PostgreSQL server, for example:\n"
    f"  export {TEST_DATABASE_URL_ENV}=postgresql+asyncpg://user:password@127.0.0.1:5432/pgclock_test\n"
    "The role needs CREATEDB: every test runs in its own database."
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

    databases: dict[bool, str] = field(default_factory=dict)


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


def drop_database(url: str) -> None:
    """Drop a database made by create_database, closing any connection a test left open."""
    name = make_url(url).database or ""
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(name)} WITH (FORCE)"))
    finally:
        admin.dispose()


# --- per-test scope, driven by tests/conftest.py ---------------------------

_session_scope = DatabaseScope()
_active_scope: DatabaseScope | None = None


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


def _drop_scope(scope: DatabaseScope) -> None:
    for url in scope.databases.values():
        drop_database(url)
    scope.databases.clear()


def scoped_database_url(*, tables: bool = True) -> str:
    """Return the current test's database URL, creating the database on first use."""
    scope = _active_scope or _session_scope
    if tables not in scope.databases:
        scope.databases[tables] = create_database(tables=tables)
    return scope.databases[tables]


def scoped_async_engine(*, tables: bool = True, **kwargs: Any) -> AsyncEngine:
    """An async engine on the current test's database (app tables included by default)."""
    return create_async_engine(scoped_database_url(tables=tables), **kwargs)


def scoped_sync_engine(*, tables: bool = False, **kwargs: Any) -> Engine:
    """A sync engine on the current test's database; empty by default, for migration tests."""
    return create_engine(_sync_url(scoped_database_url(tables=tables)), **kwargs)


@asynccontextmanager
async def isolated_database(*, create_tables: bool = False) -> AsyncIterator[IsolatedDatabase]:
    """Yield a fresh database (tables only on request); drop it afterwards."""
    url = await asyncio.to_thread(create_database, tables=create_tables)
    engine = create_async_engine(url)
    try:
        yield IsolatedDatabase(url=url, engine=engine, Session=async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
        await asyncio.to_thread(drop_database, url)


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
