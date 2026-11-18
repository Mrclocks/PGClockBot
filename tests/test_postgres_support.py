"""The shared PostgreSQL test helpers isolate tests and clean up after them."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from sqlalchemy import func, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import BotUser, ChargeCode, Setting
from tests import postgres_support
from tests.postgres_support import (
    GENERATED_ID_START,
    TEST_DATABASE_URL_ENV,
    MissingTestDatabaseError,
    PostgresTestCase,
    _quoted,
    add_shop_users,
    isolated_database,
    postgres_test_url,
    scoped_async_engine,
    scoped_database_url,
    scoped_sync_engine,
)


async def _scalar(url: str, statement: str, **params: object) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(statement), params)).scalar()
    finally:
        await engine.dispose()


async def _database_exists(url: str) -> bool:
    name = make_url(url).database
    count = await _scalar(postgres_test_url(), "SELECT count(*) FROM pg_database WHERE datname = :name", name=name)
    return count == 1


async def _table_count(url: str) -> object:
    return await _scalar(url, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")


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
        for hostile in ("public", 'pgct_db_x"; DROP DATABASE postgres; --', "pgct_db_a b"):
            with self.assertRaises(ValueError):
                _quoted(hostile)

    def test_app_engine_points_at_the_test_server(self) -> None:
        self.assertEqual(make_url(os.environ["DATABASE_URL"]).host, make_url(postgres_test_url()).host)


class IsolatedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_databases_do_not_see_each_others_rows(self) -> None:
        async with isolated_database(create_tables=True) as first, isolated_database(create_tables=True) as second:
            async with first.Session() as session:
                session.add(Setting(key="only_in_first", value="1"))
                await session.commit()
            async with second.Session() as session:
                self.assertEqual(await session.scalar(select(func.count()).select_from(Setting)), 0)

    async def test_database_is_dropped_when_the_test_body_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            async with isolated_database() as database:
                url = database.url
                raise RuntimeError("test body failed")
        self.assertFalse(await _database_exists(url))

    async def test_database_starts_empty_unless_tables_are_requested(self) -> None:
        async with isolated_database() as empty, isolated_database(create_tables=True) as full:
            self.assertEqual(await _table_count(empty.url), 0)
            self.assertEqual(await _scalar(full.url, "SELECT count(*) FROM settings"), 0)

    async def test_concurrent_sessions_see_each_others_commits(self) -> None:
        async with isolated_database(create_tables=True) as database:
            async def write(key: str) -> None:
                async with database.Session() as session:
                    session.add(Setting(key=key, value="1"))
                    await session.commit()

            await asyncio.gather(*(write(f"key_{index}") for index in range(5)))
            async with database.Session() as session:
                self.assertEqual(await session.scalar(select(func.count()).select_from(Setting)), 5)


class ScopedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_calls_in_one_test_share_the_database(self) -> None:
        first = scoped_async_engine()
        second = scoped_async_engine()
        try:
            async with first.begin() as conn:
                await conn.execute(text("INSERT INTO settings (key, value) VALUES ('shared', '1')"))
            async with second.connect() as conn:
                self.assertEqual(await conn.scalar(text("SELECT value FROM settings WHERE key = 'shared'")), "1")
        finally:
            await first.dispose()
            await second.dispose()

    async def test_a_different_key_gives_a_separate_database(self) -> None:
        self.assertNotEqual(scoped_database_url(), scoped_database_url(key="second"))
        self.assertEqual(scoped_database_url(key="second"), scoped_database_url(key="second"))

    async def test_generated_ids_do_not_collide_with_seeded_ones(self) -> None:
        engine = scoped_async_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as session:
                session.add(Setting(id=1, key="seeded", value="1"))
                await session.commit()
                generated = Setting(key="generated", value="1")
                session.add(generated)
                await session.commit()
            self.assertEqual(generated.id, GENERATED_ID_START)
        finally:
            await engine.dispose()

    async def test_shop_users_satisfy_shop_foreign_keys(self) -> None:
        engine = scoped_async_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await add_shop_users(sessions, 41, 42)
            async with sessions() as session:
                session.add(ChargeCode(code="SHOP41", amount=1, reseller_id=41))
                await session.commit()
                roles = (await session.scalars(select(BotUser.role).where(BotUser.id.in_([41, 42])))).all()
            self.assertEqual(roles, ["reseller", "reseller"])
        finally:
            await engine.dispose()

    def test_sync_engine_starts_empty_for_migration_tests(self) -> None:
        engine = scoped_sync_engine()
        try:
            with engine.connect() as conn:
                tables = conn.scalar(
                    text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
                )
        finally:
            engine.dispose()
        self.assertEqual(tables, 0)

    async def test_scope_end_drops_the_database_and_open_connections(self) -> None:
        scope = postgres_support.begin_scope()
        url = scoped_database_url()
        engine = create_async_engine(url)
        conn = await engine.connect()
        try:
            await asyncio.to_thread(postgres_support.end_scope, scope)
            self.assertFalse(await _database_exists(url))
        finally:
            await conn.close()
            await engine.dispose()


class PostgresTestCaseTests(PostgresTestCase):
    async def test_each_test_gets_a_database_with_the_app_tables(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key="case_key", value="1"))
            await session.commit()
            self.assertEqual(await session.scalar(select(Setting.value).where(Setting.key == "case_key")), "1")
