"""Isolated PostgreSQL databases for tests.

Tests run against a real PostgreSQL server named by ``TEST_DATABASE_URL``.
Each test gets its own throwaway namespace so tests stay independent, can
commit, can open concurrent sessions, and can run in parallel under xdist:

* ``isolated_schema`` / ``PostgresTestCase``: a fresh schema reached through
  ``search_path``. Cheap; the default for service and handler tests.
* ``isolated_database``: a fresh database. Use it when the code under test
  only receives a URL (Alembic migrations, CLI commands, the app engine),
  because asyncpg cannot carry ``search_path`` inside a URL.

A missing ``TEST_DATABASE_URL`` is an error, not a skip, so a test can never
pass silently without a database behind it.
"""

from __future__ import annotations

import os
import secrets
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import Base

# Importing the models registers every table on Base.metadata.
import app.db.models  # noqa: F401

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
SCHEMA_PREFIX = "pgct_"
DATABASE_PREFIX = "pgct_db_"
_NAME_TOKEN_BYTES = 8

MISSING_URL_MESSAGE = (
    f"{TEST_DATABASE_URL_ENV} is not set. Tests need a PostgreSQL server, for example:\n"
    f"  export {TEST_DATABASE_URL_ENV}=postgresql+asyncpg://user:password@127.0.0.1:5432/pgclock_test\n"
    "The role needs CREATE on that database (schemas) and CREATEDB (migration tests)."
)


class MissingTestDatabaseError(RuntimeError):
    """Raised when a database test runs without TEST_DATABASE_URL."""


@dataclass(slots=True)
class IsolatedDatabase:
    """A throwaway schema or database and the handles a test needs."""

    url: str
    engine: AsyncEngine
    Session: async_sessionmaker[AsyncSession]
    schema: str | None = None


def postgres_test_url() -> str:
    """Return the asyncpg URL of the test server, or fail loudly."""
    raw = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not raw:
        raise MissingTestDatabaseError(MISSING_URL_MESSAGE)
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise MissingTestDatabaseError(f"{TEST_DATABASE_URL_ENV} must point at PostgreSQL")
    return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def _unique_name(prefix: str) -> str:
    return prefix + secrets.token_hex(_NAME_TOKEN_BYTES)


def _quoted(identifier: str) -> str:
    # Names come only from _unique_name (prefix + hex), never from test input.
    if not identifier.startswith((SCHEMA_PREFIX, DATABASE_PREFIX)) or not identifier.replace("_", "").isalnum():
        raise ValueError("refusing to quote an identifier that was not generated here")
    return f'"{identifier}"'


async def _create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def isolated_schema(*, create_tables: bool = True) -> AsyncIterator[IsolatedDatabase]:
    """Yield an engine whose connections live in a fresh schema; drop it afterwards."""
    url = postgres_test_url()
    schema = _unique_name(SCHEMA_PREFIX)
    admin = create_async_engine(url, poolclass=NullPool)
    engine: AsyncEngine | None = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {_quoted(schema)}"))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        if create_tables:
            await _create_tables(engine)
        yield IsolatedDatabase(
            url=url,
            engine=engine,
            Session=async_sessionmaker(engine, expire_on_commit=False),
            schema=schema,
        )
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_quoted(schema)} CASCADE"))
        await admin.dispose()


@asynccontextmanager
async def isolated_database(*, create_tables: bool = False) -> AsyncIterator[IsolatedDatabase]:
    """Yield a fresh, empty database (tables only on request); drop it afterwards."""
    server_url = make_url(postgres_test_url())
    name = _unique_name(DATABASE_PREFIX)
    admin = create_async_engine(server_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    url = server_url.set(database=name).render_as_string(hide_password=False)
    engine: AsyncEngine | None = None
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE {_quoted(name)}"))
        engine = create_async_engine(url)
        if create_tables:
            await _create_tables(engine)
        yield IsolatedDatabase(url=url, engine=engine, Session=async_sessionmaker(engine, expire_on_commit=False))
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {_quoted(name)} WITH (FORCE)"))
        await admin.dispose()


class PostgresTestCase(unittest.IsolatedAsyncioTestCase):
    """Base class giving every test ``self.engine`` and ``self.Session`` in its own schema."""

    create_tables: ClassVar[bool] = True

    engine: AsyncEngine
    Session: async_sessionmaker[AsyncSession]
    database: IsolatedDatabase

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        context = isolated_schema(create_tables=self.create_tables)
        self.database = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.engine = self.database.engine
        self.Session = self.database.Session
