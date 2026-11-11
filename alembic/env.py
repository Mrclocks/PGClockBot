"""Alembic environment — sync migrations (safe from async app startup)."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text

from app.config import get_settings
from app.db import Base
from app.db.engine_url import to_sync_url

import app.db.models  # noqa: F401

config = context.config
# Only the alembic CLI configures logging. Run from the app (alembic_runner), fileConfig
# would disable every logger already created and replace the app's own log setup.
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic's default alembic_version.version_num is VARCHAR(32). Several
# revision ids in this tree are longer (e.g. 0015_org_principal_web_identities).
# SQLite ignores the limit; PostgreSQL truncates/errors — widen before migrate.
_ALEMBIC_VERSION_NUM_LEN = 128


def get_sync_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url or url.startswith("driver://"):
        url = get_settings().database_url
    return to_sync_url(url)


def _ensure_alembic_version_num_width(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    insp = inspect(connection)
    if not insp.has_table("alembic_version"):
        connection.execute(
            text(
                "CREATE TABLE alembic_version ("
                f"version_num VARCHAR({_ALEMBIC_VERSION_NUM_LEN}) NOT NULL"
                ")"
            )
        )
        connection.commit()
        return
    connection.execute(
        text(
            "ALTER TABLE alembic_version "
            f"ALTER COLUMN version_num TYPE VARCHAR({_ALEMBIC_VERSION_NUM_LEN})"
        )
    )
    connection.commit()


def run_migrations_offline() -> None:
    url = get_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _ensure_alembic_version_num_width(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
