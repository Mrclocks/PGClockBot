"""Ownership migration fills legacy identities and retains populated channel data."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import test_customer_identity_migration as identity

connection = identity.connection
SPEC = importlib.util.spec_from_file_location("ownership_migration", Path(__file__).resolve().parents[1] / "alembic/versions/0075_service_channel_ownership.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def prepare(connection) -> None:
    identity.prepare(connection)
    identity.MIGRATION.upgrade()
    # The existing-data fixture deliberately represents only the service columns
    # relevant to Phase 2A. These are the other preserved production columns.
    for name, sqltype in (("owner_principal_id", "INTEGER"), ("subscription_url", "TEXT"), ("subscription_token", "TEXT")):
        connection.exec_driver_sql(f"ALTER TABLE user_services ADD COLUMN {name} {sqltype}")
    connection.commit()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")


def test_incomplete_ownership_is_backfilled_automatically(connection: sa.engine.Connection) -> None:
    prepare(connection)
    MIGRATION.upgrade()
    customer = connection.execute(sa.text("SELECT id,legacy_bot_user_id FROM customers")).one()
    service = connection.execute(sa.text("SELECT customer_id,public_id FROM user_services")).one()
    assert customer.legacy_bot_user_id == 1 and service.customer_id == customer.id
    assert service.public_id.startswith("svc_")
    MIGRATION.upgrade()
    assert service == connection.execute(sa.text("SELECT customer_id,public_id FROM user_services")).one()
    assert connection.scalar(sa.text("SELECT count(*) FROM customers")) == 1
    assert connection.scalar(sa.text("SELECT amount FROM synthetic_orders")) == 999


def test_legacy_customer_backfill_respects_sqlite_bind_budget_and_keeps_ids() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            metadata = sa.MetaData()
            users = sa.Table("bot_users", metadata, sa.Column("id", sa.Integer, primary_key=True),
                sa.Column("is_blocked", sa.Boolean, nullable=False))
            customers = sa.Table("customers", metadata, sa.Column("id", sa.Integer, primary_key=True),
                sa.Column("public_id", sa.String(40), nullable=False, unique=True),
                sa.Column("legacy_bot_user_id", sa.Integer, unique=True),
                sa.Column("state", sa.String(16), nullable=False))
            metadata.create_all(connection)
            connection.execute(users.insert(), [{"id": n, "is_blocked": n == 1} for n in range(1, 501)])
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION._backfill_customers()
                connection.execute(customers.update().where(customers.c.legacy_bot_user_id == 2).values(state="blocked"))
                before = connection.execute(customers.select().order_by(customers.c.id).limit(501)).all()
                MIGRATION._backfill_customers()
            assert len(before) == 500
            assert before == connection.execute(customers.select().order_by(customers.c.id).limit(501)).all()
            assert connection.scalar(sa.select(customers.c.state).where(customers.c.legacy_bot_user_id == 1)) == "blocked"
            assert connection.scalar(sa.select(customers.c.state).where(customers.c.legacy_bot_user_id == 2)) == "blocked"
    finally:
        engine.dispose()


def test_conflicting_ownership_refuses_before_schema_change(connection: sa.engine.Connection) -> None:
    prepare(connection)
    connection.execute(identity.reflected(connection, "customers").insert(),
        {"id": 10, "public_id": "cus_other", "legacy_bot_user_id": None})
    connection.execute(sa.text("UPDATE user_services SET customer_id=10"))
    before = [(c["name"], c["nullable"], str(c["type"])) for c in sa.inspect(connection).get_columns("user_services")]
    with pytest.raises(RuntimeError, match="ownership conflicts"):
        MIGRATION.upgrade()
    assert before == [(c["name"], c["nullable"], str(c["type"])) for c in sa.inspect(connection).get_columns("user_services")]
    assert connection.scalar(sa.text("SELECT amount FROM synthetic_orders")) == 999


def test_repeat_upgrade_and_populated_downgrade_keep_both_channels(connection) -> None:
    prepare(connection)
    customers = identity.reflected(connection, "customers")
    connection.execute(customers.insert(), [{"id": 1, "public_id": "cus_legacy", "legacy_bot_user_id": 1},
                                          {"id": 2, "public_id": "cus_web", "legacy_bot_user_id": None}])
    connection.execute(sa.text("UPDATE user_services SET customer_id=1,public_id='svc_legacy'"))
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    services = identity.reflected(connection, "user_services")
    connection.execute(services.insert(), {"id": 2, "bot_user_id": None, "customer_id": 2, "public_id": "svc_web",
        "integration_id": "intg_synthetic", "origin_operation_id": "op_synthetic", "owner_principal_id": 1,
        "shop_needs_review": False, "pg_user_id": 202})
    before = connection.execute(services.select().order_by(services.c.id)).all()
    for change in ({"bot_user_id": 1}, {"subscription_url": "https://example.invalid/private"},
                   {"origin_operation_id": None}, {"customer_id": None}, {"shop_needs_review": True}):
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(services.update().where(services.c.id == 2).values(**change))
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    MIGRATION.upgrade()
    assert before == connection.execute(services.select().order_by(services.c.id)).all()
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    assert connection.scalar(sa.text("SELECT amount FROM synthetic_orders")) == 999
    assert next(c for c in sa.inspect(connection).get_columns("user_services") if c["name"] == "bot_user_id")["nullable"]
    if connection.dialect.name == "sqlite":
        assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()


def test_ownership_upgrade_preserves_username_lookup_index(connection: sa.engine.Connection) -> None:
    prepare(connection)
    connection.exec_driver_sql("ALTER TABLE user_services ADD COLUMN pg_username VARCHAR(128)")
    connection.exec_driver_sql("CREATE INDEX ix_user_services_pg_username_lower ON user_services (lower(pg_username))")
    connection.execute(identity.reflected(connection, "customers").insert(),
        {"id": 1, "public_id": "cus_legacy", "legacy_bot_user_id": 1})
    connection.execute(sa.text("UPDATE user_services SET customer_id=1,public_id='svc_legacy',pg_username='Synthetic'"))
    for operation in (MIGRATION.upgrade, MIGRATION.upgrade, MIGRATION.downgrade, MIGRATION.upgrade):
        operation()
        assert connection.scalar(sa.text("SELECT pg_username FROM user_services WHERE lower(pg_username)='synthetic'")) == "Synthetic"
        if connection.dialect.name == "sqlite":
            plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM user_services WHERE lower(pg_username)='synthetic'").all()
            assert any("ix_user_services_pg_username_lower" in row[3] for row in plan)
        else:
            assert "ix_user_services_pg_username_lower" in {
                index["name"] for index in sa.inspect(connection).get_indexes("user_services")}
