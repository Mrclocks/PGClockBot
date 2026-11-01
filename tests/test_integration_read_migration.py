"""Read schema replay and rollback retain populated website snapshots and request keys."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa

import test_customer_identity_migration as identity
import test_integration_ownership_migration as ownership

connection = identity.connection
SPEC = importlib.util.spec_from_file_location("read_migration", Path(__file__).resolve().parents[1] / "alembic/versions/0077_integration_read_snapshots.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def test_populated_web_snapshot_and_request_key_survive_upgrade_twice_and_downgrade(connection):
    ownership.prepare(connection)
    customers = identity.reflected(connection, "customers")
    connection.execute(customers.insert(), [{"id": 1, "public_id": "cus_legacy", "legacy_bot_user_id": 1},
                                          {"id": 2, "public_id": "cus_web", "legacy_bot_user_id": None}])
    connection.execute(sa.text("UPDATE user_services SET customer_id=1,public_id='svc_legacy'"))
    ownership.MIGRATION.upgrade()
    services = identity.reflected(connection, "user_services")
    connection.execute(services.insert(), {"id": 2, "customer_id": 2, "public_id": "svc_web", "integration_id": "intg_synthetic",
        "origin_operation_id": "op_synthetic", "owner_principal_id": 1, "pg_user_id": 701, "shop_needs_review": False})
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    now = datetime.now(timezone.utc)
    catalog = identity.reflected(connection, "integration_catalog_plans")
    snapshots = identity.reflected(connection, "integration_service_snapshots")
    replays = identity.reflected(connection, "integration_request_replays")
    connection.execute(catalog.insert(), {"integration_id": "intg_synthetic", "plan_id": 1, "public_id": "pln_synthetic"})
    connection.execute(snapshots.insert(), {"service_id": 2, "state": "review", "used_bytes": 123, "synced_at": now})
    connection.execute(replays.insert(), {"integration_id": "intg_synthetic", "key_digest": "k"*64, "input_digest": "i"*64,
        "resource_kind": "customer", "resource_id": "cus_web", "expires_at": now+timedelta(days=1)})
    tables = [catalog, snapshots, replays, services]
    before = [connection.execute(table.select()).all() for table in tables]
    MIGRATION.downgrade()
    MIGRATION.downgrade()
    MIGRATION.upgrade()
    MIGRATION.upgrade()
    assert before == [connection.execute(table.select()).all() for table in tables]
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    assert connection.scalar(sa.text("SELECT amount FROM synthetic_orders")) == 999
