"""Ordinary application startup upgrades populated legacy databases without a CLI backfill."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from shutil import copyfile
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import session as database
from app.db.alembic_runner import alembic_config, current_revision, heads
from app.db.engine_url import normalize_async_url, parse_engine


def legacy_metadata() -> sa.MetaData:
    """The baseline revision uses live metadata; supply its pre-integration shape."""
    metadata = sa.MetaData()
    excluded_tables = {"customers", "customer_telegram_links", "customer_link_challenges", "customer_link_actions", "service_mutation_guards"}
    excluded_columns = {"customer_id", "public_id", "integration_id", "origin_operation_id"}
    for table in database.Base.metadata.sorted_tables:
        if table.name.startswith("integration") or table.name in excluded_tables:
            continue
        if table.name != "user_services":
            table.to_metadata(metadata)
            continue
        sa.Table(table.name, metadata, *[
            sa.Column(column.name, column.type, *[
                sa.ForeignKey(foreign.target_fullname, ondelete=foreign.ondelete)
                for foreign in column.foreign_keys
            ], primary_key=column.primary_key, nullable=False if column.name == "bot_user_id" else column.nullable,
                server_default=column.server_default)
            for column in table.columns if column.name not in excluded_columns
        ])
    return metadata


def legacy_alembic_config(database_url: str, directory: Path) -> Config:
    config = alembic_config(database_url)
    scripts = ScriptDirectory.from_config(config)
    versions = directory / "versions"
    versions.mkdir(parents=True)
    copyfile(Path(scripts.dir) / "env.py", directory / "env.py")
    for revision in scripts.walk_revisions(base="base", head="0070_shop_miniapp_key"):
        copyfile(revision.path, versions / Path(revision.path).name)
    config.set_main_option("script_location", str(directory))
    return config


def populate(connection: sa.Connection) -> dict[str, list[sa.Row]]:
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=connection) for name in (
        "bot_users", "user_services", "orders", "payments", "settings")}
    connection.execute(tables["bot_users"].insert(), [
        {"id": n, "telegram_id": 10000+n, "referral_code": f"startup-{n}", "role": "user",
         "wallet_balance": n, "points_balance": 0, "is_blocked": n == 100}
        for n in range(100, 603)
    ])
    connection.execute(tables["user_services"].insert(), [
        {"id": n, "bot_user_id": n, "pg_user_id": 1000+n, "pg_username": f"startup-{n}",
         "shop_needs_review": True, "notified_expire": False, "notified_traffic": False}
        for n in range(100, 603)
    ])
    connection.execute(tables["orders"].insert(), {
        "id": 1, "user_id": 100, "service_id": 100, "status": "delivered", "amount": 123,
        "quantity": 1, "is_renewal": False,
        "discount_amount": 0,
    })
    connection.execute(tables["payments"].insert(), {
        "id": 1, "order_id": 1, "user_id": 100, "amount": 123, "method": "wallet", "status": "approved",
        "is_wallet_topup": False,
    })
    connection.execute(tables["settings"].insert(), {"key": "integration_api_enabled", "value": "0"})
    return {name: connection.execute(table.select().order_by(*table.primary_key)).all()
            for name, table in tables.items()}


async def start(database_url: str) -> None:
    async_url = sa.engine.make_url(normalize_async_url(database_url.replace(
        "postgresql+psycopg:", "postgresql+asyncpg:")))
    options = async_url.query.get("options")
    engine_options = {"connect_args": {"server_settings": {"search_path": options.removeprefix("-csearch_path=")}}} if options else {}
    engine = create_async_engine(async_url.difference_update_query(["options"]), **engine_options)
    try:
        with patch.multiple(database, engine=engine, _db_url=database_url,
                            _engine_info=parse_engine(str(async_url)), _ALLOW_CREATE_ALL=False):
            await database.init_db()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_startup_upgrades_0070_with_users_services_and_payments_without_manual_steps(
    dialect: str, tmp_path: Path,
) -> None:
    schema = None
    if dialect == "postgresql":
        url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select a disposable synthetic database")
        schema = f"startup_{uuid4().hex}"
    else:
        url = f"sqlite:///{tmp_path / 'startup.db'}"
    engine = sa.create_engine(url)
    if schema:
        with engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        scoped_url = sa.engine.make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
        migration_url = f"{url}{'&' if '?' in url else '?'}options=-csearch_path={schema}"
        scoped = sa.create_engine(scoped_url)
    else:
        migration_url, scoped = url, engine
    config = alembic_config(migration_url)
    try:
        with patch.object(database.Base, "metadata", legacy_metadata()):
            command.upgrade(config, "0070_shop_miniapp_key")
        with scoped.begin() as connection:
            assert "customer_id" not in {column["name"] for column in sa.inspect(connection).get_columns("user_services")}
            before = populate(connection)
            legacy_columns = {name: sa.Table(name, sa.MetaData(), autoload_with=connection)
                              for name in before}
        asyncio.run(start(migration_url))
        assert current_revision(migration_url) == heads()[0]
        with scoped.connect() as connection:
            for name, table in legacy_columns.items():
                assert before[name] == connection.execute(table.select().order_by(*table.primary_key)).all()
            identities = connection.execute(sa.text(
                "SELECT id,public_id,state,legacy_bot_user_id FROM customers ORDER BY id")).all()
            services = connection.execute(sa.text(
                "SELECT id,customer_id,public_id FROM user_services ORDER BY id")).all()
            assert len(identities) == len(services) == 503
            owners = {row.legacy_bot_user_id: row.id for row in identities}
            assert all(row.customer_id == owners[row.id] and row.public_id for row in services)
            assert next(row.state for row in identities if row.legacy_bot_user_id == 100) == "blocked"
            if dialect == "sqlite":
                assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        asyncio.run(start(migration_url))
        command.downgrade(config, "0073_integration_security")
        asyncio.run(start(migration_url))
        with scoped.connect() as connection:
            assert identities == connection.execute(sa.text(
                "SELECT id,public_id,state,legacy_bot_user_id FROM customers ORDER BY id")).all()
            assert services == connection.execute(sa.text(
                "SELECT id,customer_id,public_id FROM user_services ORDER BY id")).all()
    finally:
        if scoped is not engine:
            scoped.dispose()
        if schema:
            with engine.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


def test_never_activated_rollback_prepares_revision_and_preserves_legacy_service_writes(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'rollback.db'}"
    config = alembic_config(url)
    with patch.object(database.Base, "metadata", legacy_metadata()):
        command.upgrade(config, "0070_shop_miniapp_key")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            before = populate(connection)
            legacy_tables = {name: sa.Table(name, sa.MetaData(), autoload_with=connection) for name in before}
        command.upgrade(config, "head")
        old_config = legacy_alembic_config(url, tmp_path / "old_alembic")
        with pytest.raises(CommandError, match="Can't locate revision"):
            command.upgrade(old_config, "head")
        command.downgrade(config, "0070_shop_miniapp_key")
        command.upgrade(old_config, "head")
        assert current_revision(url) == "0070_shop_miniapp_key"
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            for name, table in legacy_tables.items():
                assert before[name] == connection.execute(table.select().order_by(*table.primary_key)).all()
            connection.execute(legacy_tables["user_services"].insert(), {
                "id": 603, "bot_user_id": 100, "pg_user_id": 1603, "pg_username": "rollback-603",
                "shop_needs_review": False, "notified_expire": False, "notified_traffic": False,
            })
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM user_services")) == 504
            assert connection.scalar(sa.text("SELECT customer_id FROM user_services WHERE id=603")) is not None
            assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
