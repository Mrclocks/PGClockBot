"""Populated durable operations, private access and outbox survive repeated migrations."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

import test_customer_identity_migration as identity
import test_integration_ownership_migration as ownership
import test_integration_read_migration as reads

connection = identity.connection
ROOT = Path(__file__).resolve().parents[1]


def migration(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"alembic/versions/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATIONS = tuple(migration(name) for name in ("0078_integration_operations", "0079_integration_private_delivery"))


def test_populated_repeat_upgrade_index_repair_and_retaining_downgrade(connection):
    ownership.prepare(connection)
    customers = identity.reflected(connection, "customers")
    connection.execute(customers.insert(), [{"id": 1, "public_id": "cus_legacy", "legacy_bot_user_id": 1},
        {"id": 2, "public_id": "cus_web", "legacy_bot_user_id": None}])
    connection.execute(sa.text("UPDATE user_services SET customer_id=1,public_id='svc_legacy'"))
    ownership.MIGRATION.upgrade()
    services = identity.reflected(connection, "user_services")
    connection.execute(services.insert(), {"id": 2, "customer_id": 2, "public_id": "svc_web", "integration_id": "intg_synthetic",
        "origin_operation_id": "op_synthetic", "owner_principal_id": 1, "pg_user_id": 701, "shop_needs_review": False})
    sa.Table("service_addon_packs", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True)).create(connection)
    connection.execute(sa.text("INSERT INTO service_addon_packs (id) VALUES (1)"))
    reads.MIGRATION.upgrade()
    for module in MIGRATIONS:
        module.upgrade()
        module.upgrade()
    tables = {name: identity.reflected(connection, name) for name in (
        "integration_operations", "integration_operation_audit", "integration_native_mutations", "service_mutation_guards",
        "integration_capacity_commitments", "integration_deliveries", "integration_addon_grants")}
    now = datetime.now(timezone.utc)
    connection.execute(tables["integration_operations"].insert(), {"id": 1, "public_id": "op_synthetic", "integration_id": "intg_synthetic",
        "customer_id": 2, "service_id": 2, "accepting_credential_selector": "c"*32, "external_ref": "synthetic-ref",
        "external_ref_digest": "d"*64, "input_digest": "i"*64, "input_json": "{}", "accepted_terms_json": "{}", "kind": "provision",
        "state": "outcome_unknown", "target_json": "{}", "dispatch_intent_at": now, "evidence_hold": True})
    connection.execute(tables["integration_capacity_commitments"].insert(), {"operation_id": 1, "principal_id": 1, "bytes": 999, "user_slots": 1, "active": True})
    connection.execute(tables["service_mutation_guards"].insert(), {"service_id": 2, "operation_id": 1})
    connection.execute(tables["integration_deliveries"].insert(), {"service_id": 2, "url_enc": "synthetic-ciphertext", "url_digest": "u"*64, "validated_at": now})
    connection.execute(tables["integration_addon_grants"].insert(), {"integration_id": "intg_synthetic", "pack_id": 1, "public_id": "pack_synthetic", "enabled": True})
    before = {name: connection.execute(table.select()).all() for name, table in tables.items()}
    for module in reversed(MIGRATIONS):
        module.downgrade()
        module.downgrade()
    for module in MIGRATIONS:
        module.upgrade()
        module.upgrade()
    assert before == {name: connection.execute(table.select()).all() for name, table in tables.items()}
    assert connection.scalar(sa.text("SELECT value FROM settings WHERE key='integration_api_enabled'")) == "0"
    assert connection.scalar(sa.text("SELECT amount FROM synthetic_orders")) == 999
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(tables["integration_capacity_commitments"].insert(), {"operation_id": 1, "principal_id": 1, "bytes": 999, "user_slots": 1})
    with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(tables["service_mutation_guards"].insert(), {"service_id": 1})
