"""Repeat-safe foundation migrations against isolated SQLite and PostgreSQL schemas."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import Connection

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("integration_migration", ROOT / "alembic/versions/0071_integration_foundation.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


@pytest.fixture(params=["sqlite", "postgresql"])
def connection(request: pytest.FixtureRequest) -> Iterator[Connection]:
    if request.param == "postgresql":
        url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select an isolated synthetic test database")
    else:
        url = "sqlite:///:memory:"
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        schema = f"phase1a_{uuid4().hex}" if request.param == "postgresql" else None
        if schema:
            conn.execute(sa.schema.CreateSchema(schema))
            conn.execute(sa.text("SELECT set_config('search_path', :schema, false)"), {"schema": schema})
        else:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()
        metadata = sa.MetaData()
        sa.Table("bot_users", metadata, sa.Column("id", sa.Integer, primary_key=True),
                 sa.Column("is_blocked", sa.Boolean, nullable=False, server_default=sa.false()),
                 sa.Column("telegram_id", sa.BigInteger, nullable=False),
                 sa.Column("wallet_balance", sa.Integer, nullable=False))
        sa.Table("org_principals", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("plans", metadata, sa.Column("id", sa.Integer, primary_key=True))
        sa.Table("settings", metadata, sa.Column("key", sa.String(128), primary_key=True), sa.Column("value", sa.Text))
        metadata.create_all(conn)
        conn.execute(metadata.tables["bot_users"].insert(), {"id": 1, "telegram_id": 10001, "wallet_balance": 321})
        conn.execute(metadata.tables["org_principals"].insert(), {"id": 1})
        conn.execute(metadata.tables["plans"].insert(), {"id": 1})
        conn.execute(metadata.tables["settings"].insert(), {"key": "integration_api_enabled", "value": "1"})
        conn.commit()
        try:
            conn.info["migration_output"] = StringIO()
            with Operations.context(MigrationContext.configure(conn, opts={"output_buffer": conn.info["migration_output"]})):
                yield conn
        finally:
            conn.rollback()
            if schema:
                conn.execute(sa.schema.DropSchema(schema, cascade=True))
                conn.commit()
    engine.dispose()


def foundation_tables(connection: Connection) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=connection) for name in MIGRATION.TABLES}


def assert_legacy_preserved(connection: Connection) -> None:
    assert connection.execute(sa.text("SELECT id, telegram_id, wallet_balance FROM bot_users")).one() == (1, 10001, 321)


def test_unused_foundation_removal_and_reupgrade_preserve_existing_rows(connection: Connection) -> None:
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert set(MIGRATION.TABLES) <= set(sa.inspect(connection).get_table_names())
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    assert not set(MIGRATION.TABLES) & set(sa.inspect(connection).get_table_names())
    assert_legacy_preserved(connection)
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert set(MIGRATION.TABLES) <= set(sa.inspect(connection).get_table_names())
    assert_legacy_preserved(connection)


def test_downgrade_keeps_empty_foundation_referenced_by_retained_services(connection: Connection) -> None:
    MIGRATION.upgrade()
    metadata = sa.MetaData()
    sa.Table("bot_users", metadata, autoload_with=connection)
    sa.Table("integrations", metadata, autoload_with=connection)
    services = sa.Table(
        "synthetic_services", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("bot_user_id", sa.Integer, sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=True),
    )
    services.create(connection)
    connection.execute(services.insert(), {"id": 1, "bot_user_id": 1})
    before = connection.execute(services.select()).all()
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    assert set(MIGRATION.TABLES) <= set(sa.inspect(connection).get_table_names())
    assert before == connection.execute(services.select()).all()
    connection.execute(services.insert(), {"id": 2, "bot_user_id": 1})
    assert connection.scalar(sa.select(sa.func.count()).select_from(services)) == 2
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert connection.scalar(sa.select(sa.func.count()).select_from(services)) == 2
    assert_legacy_preserved(connection)


def test_populated_downgrade_retains_all_foundation_data_and_reports_retention(connection: Connection) -> None:
    MIGRATION.upgrade()
    tables = foundation_tables(connection)
    now = datetime.now(timezone.utc)
    connection.execute(tables["integrations"].insert(), {
        "id": "intg_synthetic", "owner_principal_id": 1, "scope_kind": "platform", "shop_id": None,
        "status": "active", "grants": "plans:read",
    })
    connection.execute(tables["integration_plans"].insert(), {"integration_id": "intg_synthetic", "plan_id": 1})
    connection.execute(tables["integration_credentials"].insert(), {
        "selector": "a"*32, "integration_id": "intg_synthetic", "secret_digest": "b"*64,
        "scopes": "plans:read", "created_at": now, "expires_at": now+timedelta(days=90),
    })
    before = {name: connection.execute(table.select()).all() for name, table in tables.items()}
    with patch.object(MIGRATION.logger, "disabled", True):
        MIGRATION.downgrade()
        MIGRATION.downgrade()
    assert "retains populated schema" in connection.info["migration_output"].getvalue()
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    after = {name: connection.execute(table.select()).all() for name, table in tables.items()}
    assert before == after
    assert_legacy_preserved(connection)


def test_upgrade_repairs_missing_indexes_after_retained_downgrade(connection: Connection) -> None:
    MIGRATION.upgrade()
    for table, name, columns in MIGRATION.INDEXES:
        reflected = sa.Table(table, sa.MetaData(), autoload_with=connection)
        sa.Index(name, *(reflected.c[column] for column in columns)).drop(connection)
    MIGRATION.upgrade()
    for table, name, _ in MIGRATION.INDEXES:
        assert name in {index["name"] for index in sa.inspect(connection).get_indexes(table)}


@pytest.mark.parametrize("scope,shop", [("shop", None), ("platform", 1), ("unknown", None), ("shop", -1)])
def test_schema_rejects_unresolved_or_invalid_scope(connection: Connection, scope: str, shop: int | None) -> None:
    MIGRATION.upgrade()
    table = foundation_tables(connection)["integrations"]
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(table.insert(), {"id": "intg_invalid", "owner_principal_id": 1,
                                            "scope_kind": scope, "shop_id": shop})


def test_schema_defaults_disable_integration_and_grant_no_scopes(connection: Connection) -> None:
    MIGRATION.upgrade()
    tables = foundation_tables(connection)
    connection.execute(tables["integrations"].insert(), {"id": "intg_default", "owner_principal_id": 1,
                                                        "scope_kind": "shop", "shop_id": 1})
    row = connection.execute(tables["integrations"].select()).mappings().one()
    assert row["status"] == "disabled"
    assert row["grants"] == ""
    assert row["revision"] == 0
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(tables["integration_plans"].insert(), {"integration_id": "intg_missing", "plan_id": 1})
