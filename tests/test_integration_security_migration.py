"""Repeat-safe upgrades and data-retaining security downgrades on both backends."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

import test_integration_admin_migration as admin
import test_integration_migration as foundation

connection = foundation.connection
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("integration_security_migration", ROOT / "alembic/versions/0073_integration_security.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def tables(connection: Connection) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=connection) for name in MIGRATION.TABLES}


def test_populated_downgrade_reupgrade_preserves_foundation_admin_and_security(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    admin.populate(connection)
    base = foundation.foundation_tables(connection)
    now = datetime.now(timezone.utc)
    connection.execute(base["integration_credentials"].insert(), {
        "selector": "c"*32, "integration_id": "intg_synthetic", "secret_digest": "d"*64,
        "created_at": now, "expires_at": now + timedelta(days=90),
    })
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    security = tables(connection)
    connection.execute(security["integration_rate_counters"].insert(), {
        "subject": "intg_synthetic", "budget": "read", "window": 100, "used": 120,
    })
    connection.execute(security["integration_security_audit"].insert(), {
        "id": "synthetic-evidence", "request_id": "req_synthetic", "outcome": "succeeded",
        "integration_id": "intg_synthetic", "owner_principal_id": 1, "scope_kind": "platform", "evidence_hold": True,
    })
    all_tables = {**base, **admin.admin_tables(connection), **security}
    before = {name: connection.execute(table.select()).all() for name, table in all_tables.items()}
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    admin.MIGRATION.downgrade()
    foundation.MIGRATION.downgrade()
    assert "security downgrade retains populated schema" in connection.info["migration_output"].getvalue()
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert before == {name: connection.execute(table.select()).all() for name, table in all_tables.items()}
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    foundation.assert_legacy_preserved(connection)


def test_empty_security_removal_keeps_existing_management_data(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    admin.populate(connection)
    MIGRATION.upgrade()
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    assert not set(MIGRATION.TABLES) & set(sa.inspect(connection).get_table_names())
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert connection.scalar(sa.text("SELECT count(*) FROM integration_admin_audit")) == 1
    assert connection.scalar(sa.text("SELECT count(*) FROM integrations")) == 1
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"


def test_repeat_upgrade_repairs_indexes(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    MIGRATION.upgrade()
    for table, name, columns in MIGRATION.INDEXES:
        reflected = sa.Table(table, sa.MetaData(), autoload_with=connection)
        sa.Index(name, *(reflected.c[column] for column in columns)).drop(connection)
    MIGRATION.upgrade()
    for table, name, columns in MIGRATION.INDEXES:
        actual = next(index for index in sa.inspect(connection).get_indexes(table) if index["name"] == name)
        assert actual["column_names"] == columns


def test_anonymous_held_evidence_survives_downgrade_through_empty_foundation(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    MIGRATION.upgrade()
    audit = tables(connection)["integration_security_audit"]
    connection.execute(audit.insert(), {
        "id": "synthetic-anonymous", "request_id": "req_synthetic", "outcome": "invalid_credential", "evidence_hold": True,
    })
    MIGRATION.downgrade()
    admin.MIGRATION.downgrade()
    foundation.MIGRATION.downgrade()
    foundation.MIGRATION.upgrade()
    admin.MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert connection.scalar(sa.select(audit.c.evidence_hold)) is True
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
