"""Backfill canonical legacy ownership before allowing isolated website owners.

Downgrade disables the API and retains customer-aware schema and all associations.
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

revision = "0075_service_channel_ownership"
down_revision = "0074_customer_identity"
branch_labels = None
depends_on = None

BACKFILL_BATCH_SIZE = 500

OWNER_CHECK = (
    "(bot_user_id IS NOT NULL AND integration_id IS NULL AND origin_operation_id IS NULL) OR "
    "(bot_user_id IS NULL AND customer_id IS NOT NULL AND public_id IS NOT NULL AND "
    "integration_id IS NOT NULL AND origin_operation_id IS NOT NULL AND "
    "owner_principal_id IS NOT NULL AND NOT shop_needs_review AND "
    "subscription_url IS NULL AND subscription_token IS NULL)"
)


def _backfill_customers() -> None:
    bind = op.get_bind()
    users = sa.table("bot_users", sa.column("id"), sa.column("is_blocked"))
    customers = sa.table("customers", sa.column("public_id"), sa.column("legacy_bot_user_id"), sa.column("state"))
    insert = sqlite_insert if bind.dialect.name == "sqlite" else pg_insert
    after = 0
    while rows := bind.execute(sa.select(users.c.id, users.c.is_blocked).where(
        users.c.id > after,
    ).order_by(users.c.id).limit(BACKFILL_BATCH_SIZE)).all():
        bind.execute(insert(customers).on_conflict_do_nothing(index_elements=["legacy_bot_user_id"]), [
            {"public_id": f"cus_{uuid4()}", "legacy_bot_user_id": row.id,
             "state": "blocked" if row.is_blocked else "active"} for row in rows
        ])
        blocked = [row.id for row in rows if row.is_blocked]
        if blocked:
            bind.execute(customers.update().where(customers.c.legacy_bot_user_id.in_(blocked)).values(state="blocked"))
        after = rows[-1].id


def _backfill_services() -> None:
    bind = op.get_bind()
    services = sa.table("user_services", *(sa.column(name) for name in (
        "id", "bot_user_id", "customer_id", "public_id")))
    customers = sa.table("customers", sa.column("id"), sa.column("legacy_bot_user_id"))
    after = 0
    while rows := bind.execute(sa.select(
        services.c.id, services.c.bot_user_id, services.c.customer_id, services.c.public_id,
        customers.c.id.label("canonical_id"),
    ).outerjoin(customers, customers.c.legacy_bot_user_id == services.c.bot_user_id).where(
        services.c.id > after, services.c.bot_user_id.is_not(None),
    ).order_by(services.c.id).limit(BACKFILL_BATCH_SIZE)).all():
        if any(row.canonical_id is None or row.customer_id not in (None, row.canonical_id) for row in rows):
            raise RuntimeError("Legacy service ownership conflicts with canonical identity; review the saved association")
        bind.execute(services.update().where(
            services.c.id == sa.bindparam("row_id"), services.c.bot_user_id == sa.bindparam("owner_id"),
        ).values(customer_id=sa.bindparam("canonical_id"), public_id=sa.bindparam("stable_public_id")), [
            {"row_id": row.id, "owner_id": row.bot_user_id, "canonical_id": row.canonical_id,
             "stable_public_id": row.public_id or f"svc_{uuid4()}"} for row in rows
        ])
        after = rows[-1].id


def _assert_coverage() -> None:
    bind = op.get_bind()
    missing = bind.scalar(sa.text(
        "SELECT count(*) FROM bot_users b LEFT JOIN customers c ON c.legacy_bot_user_id=b.id WHERE c.id IS NULL"
    ))
    columns = {c["name"] for c in sa.inspect(bind).get_columns("user_services")}
    legacy_filter = "s.integration_id IS NULL AND " if "integration_id" in columns else ""
    invalid = bind.scalar(sa.text(
        "SELECT count(*) FROM user_services s LEFT JOIN customers c ON c.id=s.customer_id WHERE "
        + legacy_filter + "(s.bot_user_id IS NULL OR c.id IS NULL OR c.legacy_bot_user_id IS NULL "
        "OR c.legacy_bot_user_id<>s.bot_user_id OR s.public_id IS NULL)"
    ))
    if missing or invalid:
        raise RuntimeError("Canonical ownership is incomplete; run customer_identity_backfill from zero and resolve conflicts first")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Ownership migration needs a dedicated SQLite connection with foreign_keys=OFF; integrity is checked before return")
    _backfill_customers()
    _backfill_services()
    _assert_coverage()
    columns = {c["name"]: c for c in sa.inspect(bind).get_columns("user_services")}
    checks = {c["name"] for c in sa.inspect(bind).get_check_constraints("user_services")}
    # Rebuilding a referenced SQLite parent requires the dedicated migration connection above.
    with op.batch_alter_table("user_services") as batch:
        if "integration_id" not in columns:
            batch.add_column(sa.Column("integration_id", sa.String(41), nullable=True))
            batch.create_foreign_key("fk_service_integration", "integrations", ["integration_id"], ["id"])
        if "origin_operation_id" not in columns:
            batch.add_column(sa.Column("origin_operation_id", sa.String(40), nullable=True))
        if not columns["bot_user_id"]["nullable"]:
            batch.alter_column("bot_user_id", existing_type=sa.Integer(), nullable=True)
        if "ck_service_channel_owner" not in checks:
            batch.create_check_constraint("ck_service_channel_owner", OWNER_CHECK)
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("user_services")}
    for name, index_columns, unique in (
        ("ix_user_services_integration_id", ["integration_id"], False),
        ("uq_user_services_origin_operation_id", ["origin_operation_id"], True),
        ("ix_user_services_integration_customer_id", ["integration_id", "customer_id", "id"], False),
    ):
        if name not in indexes:
            op.create_index(name, "user_services", index_columns, unique=unique)

    # Alembic cannot reflect SQLite expression indexes during table recreation.
    if bind.dialect.name == "sqlite" and "pg_username" in columns:
        bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_services_pg_username_lower ON user_services (lower(pg_username))")
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_key_check").first():
        raise RuntimeError("Ownership migration detected invalid foreign-key evidence")


def downgrade() -> None:
    bind = op.get_bind()
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    bind.execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output(
        "Service ownership downgrade retains nullable legacy ownership and canonical website associations; API disabled. "
        "Use only a customer-aware compatibility binary; physical downgrade is unsupported."
    )
