"""Additive existing-data identity upgrades and retained rollback on SQLite and PostgreSQL."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

import test_integration_admin_migration as admin
import test_integration_migration as foundation
import test_integration_security_migration as security

connection = foundation.connection
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("customer_identity_migration", ROOT / "alembic/versions/0074_customer_identity.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def prepare(connection: Connection) -> None:
    metadata = sa.MetaData()
    sa.Table("bot_users", metadata, autoload_with=connection)
    services = sa.Table("user_services", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("bot_user_id", sa.Integer, sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("shop_id", sa.Integer, sa.ForeignKey("bot_users.id"), nullable=True),
        sa.Column("shop_needs_review", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pg_user_id", sa.Integer),
    )
    financial = sa.Table("synthetic_orders", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("service_id", sa.Integer, sa.ForeignKey("user_services.id"), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
    )
    metadata.create_all(connection)
    connection.execute(services.insert(), {"id": 1, "bot_user_id": 1, "shop_needs_review": True, "pg_user_id": 101})
    connection.execute(financial.insert(), {"id": 1, "service_id": 1, "amount": 999})
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    admin.populate(connection)
    base = foundation.foundation_tables(connection)
    now = datetime.now(timezone.utc)
    connection.execute(base["integration_credentials"].insert(), {
        "selector": "c"*32, "integration_id": "intg_synthetic", "secret_digest": "d"*64,
        "created_at": now, "expires_at": now + timedelta(days=90),
    })
    security.MIGRATION.upgrade()
    counters = security.tables(connection)["integration_rate_counters"]
    connection.execute(counters.insert(), {"subject": "intg_synthetic", "budget": "write", "window": 1, "used": 17})
    connection.execute(sa.text("INSERT INTO integration_security_audit (id,request_id,outcome,evidence_hold) "
                               "VALUES ('synthetic','req_synthetic','invalid_credential',true)"))


def reflected(connection: Connection, name: str) -> sa.Table:
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def test_existing_populated_upgrade_twice_and_retained_downgrade_reupgrade(connection: Connection) -> None:
    prepare(connection)
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    customers = reflected(connection, "customers")
    mappings = reflected(connection, "integration_customers")
    services = reflected(connection, "user_services")
    connection.execute(customers.insert(), [
        {"id": 1, "public_id": "cus_legacy", "legacy_bot_user_id": 1, "state": "blocked"},
        {"id": 2, "public_id": "cus_web", "legacy_bot_user_id": None, "state": "active"},
    ])
    connection.execute(mappings.insert(), {"integration_id": "intg_synthetic", "customer_id": 2,
                                          "external_customer_ref": "Reference:A", "display_name": "<hostile>"})
    connection.execute(services.update().values(customer_id=1, public_id="svc_stable"))
    names = ["customers", "integration_customers", "user_services", "synthetic_orders", "bot_users",
             "integrations", "integration_credentials", "integration_admin_audit", "integration_management_actions",
             "integration_security_audit", "integration_rate_counters"]
    tables = {name: reflected(connection, name) for name in names}
    before = {name: connection.execute(table.select()).all() for name, table in tables.items()}
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert before == {name: connection.execute(table.select()).all() for name, table in tables.items()}
    assert sa.inspect(connection).get_columns("user_services")[1]["nullable"] is False
    assert "retains additive schema" in connection.info["migration_output"].getvalue()
    foundation.assert_legacy_preserved(connection)


def test_defaults_and_constraints_preserve_case_sensitive_independent_mappings(connection: Connection) -> None:
    prepare(connection)
    MIGRATION.upgrade()
    assert connection.scalar(sa.text("SELECT customer_entry_policy FROM integrations")) == "denied"
    customers = reflected(connection, "customers")
    mappings = reflected(connection, "integration_customers")
    connection.execute(customers.insert(), [{"id": n, "public_id": f"cus_{n}"} for n in range(1, 5)])
    connection.execute(mappings.insert(), [
        {"integration_id": "intg_synthetic", "customer_id": 1, "external_customer_ref": "Ref"},
        {"integration_id": "intg_synthetic", "customer_id": 2, "external_customer_ref": "ref"},
    ])
    for values in (
        {"customer_id": 3, "external_customer_ref": "Ref"},
        {"customer_id": 1, "external_customer_ref": "different"},
    ):
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(mappings.insert(), {"integration_id": "intg_synthetic", **values})
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(customers.insert(), {"public_id": "cus_1"})
    connection.execute(customers.update().where(customers.c.id == 1).values(legacy_bot_user_id=1))
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(customers.update().where(customers.c.id == 2).values(legacy_bot_user_id=1))
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(reflected(connection, "integrations").update().values(customer_entry_policy="implicit"))
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(reflected(connection, "user_services").update().values(bot_user_id=None))
    assert connection.scalar(sa.select(sa.func.count()).select_from(mappings)) == 2


def test_repeat_upgrade_repairs_indexes_and_empty_downgrade_retains_expansion(connection: Connection) -> None:
    prepare(connection)
    MIGRATION.upgrade()
    for table, name, columns, unique in MIGRATION.INDEXES:
        reflected_table = reflected(connection, table)
        sa.Index(name, *(reflected_table.c[column] for column in columns)).drop(connection)
    MIGRATION.upgrade()
    for table, name, columns, unique in MIGRATION.INDEXES:
        index = next(index for index in sa.inspect(connection).get_indexes(table) if index["name"] == name)
        assert index["column_names"] == columns
        assert bool(index["unique"]) == unique
    MIGRATION.downgrade()
    MIGRATION.upgrade()
    assert {"customers", "integration_customers"} <= set(sa.inspect(connection).get_table_names())
    assert connection.scalar(sa.text("SELECT amount FROM synthetic_orders")) == 999


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_actual_alembic_chain_backfill_and_retained_rollback(dialect: str, tmp_path: Path) -> None:
    import asyncio
    import os
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.customer_backfill import backfill_legacy_customers, backfill_legacy_services

    schema = None
    if dialect == "postgresql":
        url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select a disposable synthetic database")
        schema = f"identity_chain_{uuid4().hex}"
    else:
        url = f"sqlite:///{tmp_path / 'chain.db'}"
    engine = sa.create_engine(url)
    if schema:
        with engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        migration_url = sa.engine.make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
        scoped = sa.create_engine(migration_url)
    else:
        migration_url, scoped = url, engine
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    configured_url = migration_url.render_as_string(hide_password=False) if schema else str(migration_url)
    config.set_main_option("sqlalchemy.url", configured_url.replace("%", "%%"))
    try:
        command.upgrade(config, "0073_integration_security")
        with scoped.begin() as connection:
            metadata = sa.MetaData()
            user = sa.Table("bot_users", metadata, autoload_with=connection)
            services = sa.Table("user_services", metadata, autoload_with=connection)
            settings = sa.Table("settings", metadata, autoload_with=connection)
            connection.execute(user.insert(), {"id": 10, "telegram_id": 10010, "referral_code": "chain",
                "role": "admin", "wallet_balance": 321, "points_balance": 0, "is_blocked": True})
            connection.execute(services.insert(), {"id": 10, "bot_user_id": 10, "pg_user_id": 501,
                "pg_username": "synthetic", "shop_needs_review": True, "notified_expire": False, "notified_traffic": False})
            connection.execute(settings.insert(), {"key": "integration_api_enabled", "value": "1"})
        command.upgrade(config, "0074_customer_identity")
        command.upgrade(config, "0074_customer_identity")
        async def backfill() -> None:
            async_url = url.replace("sqlite:", "sqlite+aiosqlite:").replace("postgresql+psycopg:", "postgresql+asyncpg:")
            options = {"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}
            async_engine = create_async_engine(async_url, **options)
            try:
                sessions = async_sessionmaker(async_engine, expire_on_commit=False)
                for _ in range(2):
                    async with sessions() as session:
                        await backfill_legacy_customers(session)
                        await session.commit()
                        await backfill_legacy_services(session)
                        await session.commit()
            finally:
                await async_engine.dispose()
        asyncio.run(backfill())
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with scoped.connect() as connection:
            identity = connection.execute(sa.text("SELECT public_id,state,legacy_bot_user_id FROM customers")).one()
            association = connection.execute(sa.text("SELECT id,bot_user_id,customer_id,public_id,pg_user_id,shop_needs_review FROM user_services")).one()
            assert identity.state == "blocked" and identity.legacy_bot_user_id == 10
            assert association.customer_id and association.shop_needs_review
        command.downgrade(config, "0073_integration_security")
        with scoped.connect() as connection:
            assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
        command.upgrade(config, "head")
        with scoped.connect() as connection:
            assert identity == connection.execute(sa.text("SELECT public_id,state,legacy_bot_user_id FROM customers")).one()
            assert association == connection.execute(sa.text("SELECT id,bot_user_id,customer_id,public_id,pg_user_id,shop_needs_review FROM user_services")).one()
            assert connection.scalar(sa.text("SELECT wallet_balance FROM bot_users WHERE id=10")) == 321
    finally:
        if scoped is not engine:
            scoped.dispose()
        if schema:
            with engine.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()
