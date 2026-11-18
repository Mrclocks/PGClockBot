"""The shared PostgreSQL test helpers isolate tests and clean up after them."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import Setting
from tests.postgres_support import (
    TEST_DATABASE_URL_ENV,
    MissingTestDatabaseError,
    PostgresTestCase,
    _quoted,
    isolated_database,
    isolated_schema,
    postgres_test_url,
)


async def _scalar(url: str, statement: str, **params: object) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(statement), params)).scalar()
    finally:
        await engine.dispose()


class TestDatabaseUrlTests(unittest.TestCase):
    def test_missing_url_fails_instead_of_skipping(self) -> None:
        with patch.dict(os.environ, {TEST_DATABASE_URL_ENV: ""}):
            with self.assertRaises(MissingTestDatabaseError):
                postgres_test_url()

    def test_non_postgres_url_is_rejected(self) -> None:
        with patch.dict(os.environ, {TEST_DATABASE_URL_ENV: "mysql://user:pw@localhost/db"}):
            with self.assertRaises(MissingTestDatabaseError):
                postgres_test_url()

    def test_plain_postgres_url_gets_the_async_driver(self) -> None:
        with patch.dict(os.environ, {TEST_DATABASE_URL_ENV: "postgresql://user:pw@localhost:5432/db"}):
            self.assertEqual(postgres_test_url(), "postgresql+asyncpg://user:pw@localhost:5432/db")

    def test_only_generated_names_are_interpolated(self) -> None:
        for hostile in ('public', 'pgct_x"; DROP SCHEMA public; --', "pgct_a b"):
            with self.assertRaises(ValueError):
                _quoted(hostile)


class IsolatedSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_schemas_do_not_see_each_others_rows(self) -> None:
        async with isolated_schema() as first, isolated_schema() as second:
            async with first.Session() as session:
                session.add(Setting(key="only_in_first", value="1"))
                await session.commit()
            async with second.Session() as session:
                self.assertEqual(await session.scalar(select(func.count()).select_from(Setting)), 0)

    async def test_schema_is_dropped_after_the_block(self) -> None:
        async with isolated_schema() as database:
            schema = database.schema
        remaining = await _scalar(
            postgres_test_url(),
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name = :name",
            name=schema,
        )
        self.assertEqual(remaining, 0)

    async def test_schema_is_dropped_when_the_test_body_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            async with isolated_schema() as database:
                schema = database.schema
                raise RuntimeError("test body failed")
        remaining = await _scalar(
            postgres_test_url(),
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name = :name",
            name=schema,
        )
        self.assertEqual(remaining, 0)

    async def test_concurrent_sessions_see_each_others_commits(self) -> None:
        async with isolated_schema() as database:
            async def write(key: str) -> None:
                async with database.Session() as session:
                    session.add(Setting(key=key, value="1"))
                    await session.commit()

            await asyncio.gather(*(write(f"key_{index}") for index in range(5)))
            async with database.Session() as session:
                self.assertEqual(await session.scalar(select(func.count()).select_from(Setting)), 5)

    async def test_schema_can_start_empty(self) -> None:
        async with isolated_schema(create_tables=False) as database:
            async with database.engine.connect() as conn:
                tables = await conn.scalar(
                    text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :name"),
                    {"name": database.schema},
                )
        self.assertEqual(tables, 0)


class IsolatedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_starts_empty_and_is_dropped_afterwards(self) -> None:
        async with isolated_database() as database:
            name = database.engine.url.database
            tables = await _scalar(
                database.url,
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'",
            )
            self.assertEqual(tables, 0)
        remaining = await _scalar(
            postgres_test_url(), "SELECT count(*) FROM pg_database WHERE datname = :name", name=name
        )
        self.assertEqual(remaining, 0)

    async def test_database_url_alone_reaches_the_tables(self) -> None:
        async with isolated_database(create_tables=True) as database:
            self.assertEqual(await _scalar(database.url, "SELECT count(*) FROM settings"), 0)


class PostgresTestCaseTests(PostgresTestCase):
    async def test_each_test_gets_a_schema_with_the_app_tables(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key="case_key", value="1"))
            await session.commit()
            self.assertEqual(await session.scalar(select(Setting.value).where(Setting.key == "case_key")), "1")
        self.assertTrue(self.database.schema)
