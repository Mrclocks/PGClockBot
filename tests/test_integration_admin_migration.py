"""Repeat-safe administration schema upgrades and evidence-retaining downgrades on both databases."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

import test_integration_migration as foundation

connection = foundation.connection
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("integration_admin_migration", ROOT / "alembic/versions/0072_integration_admin_audit.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def admin_tables(connection: Connection) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=connection) for name in MIGRATION.TABLES}


def populate(connection: Connection) -> None:
    base = foundation.foundation_tables(connection)
    connection.execute(base["integrations"].insert(), {"id": "intg_synthetic", "owner_principal_id": 1, "scope_kind": "platform"})
    tables = admin_tables(connection)
    values = {"actor_principal_id": 1, "scope_kind": "platform", "integration_id": "intg_synthetic", "action": "create"}
    connection.execute(tables["integration_management_actions"].insert(), {"id": "a"*32, **values})
    connection.execute(tables["integration_admin_audit"].insert(), {"action_id": "a"*32, **values, "outcome": "succeeded",
        "after": {"status": "disabled", "grants": []}})


def test_existing_foundation_upgrade_twice_populated_downgrade_and_reupgrade_keep_rows(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    populate(connection)
    tables = admin_tables(connection)
    before = {name: connection.execute(table.select()).all() for name, table in tables.items()}
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    assert "retains populated schema" in connection.info["migration_output"].getvalue()
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert before == {name: connection.execute(table.select()).all() for name, table in tables.items()}
    assert connection.scalar(sa.text("SELECT count(*) FROM integrations")) == 1
    foundation.assert_legacy_preserved(connection)


def test_empty_admin_removal_and_reupgrade_preserve_populated_foundation(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    base = foundation.foundation_tables(connection)
    connection.execute(base["integrations"].insert(), {"id": "intg_existing", "owner_principal_id": 1, "scope_kind": "platform"})
    before = connection.execute(base["integrations"].select()).all()
    MIGRATION.upgrade()
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    assert not set(MIGRATION.TABLES) & set(sa.inspect(connection).get_table_names())
    assert before == connection.execute(base["integrations"].select()).all()
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert set(MIGRATION.TABLES) <= set(sa.inspect(connection).get_table_names())
    foundation.assert_legacy_preserved(connection)


def test_repeat_upgrade_repairs_actual_pagination_and_retention_indexes(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    MIGRATION.upgrade()
    for table, name, columns in MIGRATION.INDEXES:
        reflected = sa.Table(table, sa.MetaData(), autoload_with=connection)
        sa.Index(name, *(reflected.c[column] for column in columns)).drop(connection)
    MIGRATION.upgrade()
    for table, name, columns in MIGRATION.INDEXES:
        actual = next(index for index in sa.inspect(connection).get_indexes(table) if index["name"] == name)
        assert actual["column_names"] == columns


def test_downgrade_with_only_failed_creation_evidence_retains_it_and_disables_api(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    MIGRATION.upgrade()
    tables = admin_tables(connection)
    values = {"actor_principal_id": 1, "scope_kind": "platform", "action": "create"}
    connection.execute(tables["integration_management_actions"].insert(), {"id": "b"*32, **values})
    connection.execute(tables["integration_admin_audit"].insert(), {"action_id": "b"*32, **values, "outcome": "denied",
        "error_code": "permission_denied", "evidence_hold": True})
    MIGRATION.downgrade()
    foundation.MIGRATION.downgrade()
    assert "integrations" not in sa.inspect(connection).get_table_names()
    foundation.MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert connection.scalar(sa.select(sa.func.count()).select_from(tables["integration_admin_audit"])) == 1
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"


def test_schema_uniqueness_keeps_action_replay_barrier_after_audit_pruning(connection: Connection) -> None:
    foundation.MIGRATION.upgrade()
    MIGRATION.upgrade()
    populate(connection)
    tables = admin_tables(connection)
    connection.execute(tables["integration_admin_audit"].delete())
    action = connection.execute(tables["integration_management_actions"].select()).mappings().one()
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(tables["integration_management_actions"].insert(), dict(action))
    MIGRATION.downgrade()
    MIGRATION.upgrade()
    assert connection.scalar(sa.select(sa.func.count()).select_from(tables["integration_management_actions"])) == 1
