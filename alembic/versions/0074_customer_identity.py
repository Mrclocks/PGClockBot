"""Expand neutral customer identity without changing legacy ownership or financial data.

Backfill is separately resumable: python -m app.cli.customer_identity_backfill.
Downgrade disables API access and retains the entire additive schema and its rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074_customer_identity"
down_revision = "0073_integration_security"
branch_labels = None
depends_on = None

INDEXES = (
    ("customers", "uq_customers_public_id", ["public_id"], True),
    ("customers", "uq_customers_legacy_bot_user_id", ["legacy_bot_user_id"], True),
    ("integration_customers", "ix_integration_customers_customer_id", ["customer_id"], False),
    ("user_services", "uq_user_services_public_id", ["public_id"], True),
    ("user_services", "ix_user_services_customer_id", ["customer_id"], False),
    ("user_services", "ix_user_services_shop_customer_id", ["shop_id", "customer_id", "id"], False),
)


def _create_customers() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "customers" not in tables:
        op.create_table(
            "customers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("public_id", sa.String(40), nullable=False),
            sa.Column("state", sa.String(16), nullable=False, server_default="active"),
            sa.Column("legacy_bot_user_id", sa.Integer(), sa.ForeignKey("bot_users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("state IN ('active', 'blocked')", name="ck_customer_state"),
        )
    if "integration_customers" not in tables:
        op.create_table(
            "integration_customers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=False),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("external_customer_ref", sa.String(128), nullable=False),
            sa.Column("display_name", sa.String(128), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("integration_id", "external_customer_ref", name="uq_integration_customer_ref"),
            sa.UniqueConstraint("integration_id", "customer_id", name="uq_integration_customer_identity"),
            sa.CheckConstraint("length(external_customer_ref) BETWEEN 1 AND 128", name="ck_customer_reference_length"),
            sa.CheckConstraint("length(display_name) <= 128", name="ck_customer_display_length"),
        )


def upgrade() -> None:
    bind = op.get_bind()
    _create_customers()
    service_columns = {column["name"] for column in sa.inspect(bind).get_columns("user_services")}
    if "customer_id" not in service_columns or "public_id" not in service_columns:
        with op.batch_alter_table("user_services") as batch:
            if "customer_id" not in service_columns:
                if bind.dialect.name == "sqlite":
                    # SQLite can add a nullable inline REFERENCES without rebuilding a referenced parent.
                    op.execute("ALTER TABLE user_services ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
                else:
                    batch.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
                    batch.create_foreign_key("fk_user_services_customer_id", "customers", ["customer_id"], ["id"])
            if "public_id" not in service_columns:
                batch.add_column(sa.Column("public_id", sa.String(40), nullable=True))
    integration_columns = {column["name"] for column in sa.inspect(bind).get_columns("integrations")}
    if "customer_entry_policy" not in integration_columns:
        with op.batch_alter_table("integrations") as batch:
            batch.add_column(sa.Column(
                "customer_entry_policy", sa.String(16),
                sa.CheckConstraint("customer_entry_policy IN ('denied', 'website')", name="ck_integration_customer_entry"),
                nullable=False, server_default="denied",
            ))
    for table, name, columns, unique in INDEXES:
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        if name not in indexes:
            op.create_index(name, table, columns, unique=unique)


def downgrade() -> None:
    bind = op.get_bind()
    if "settings" in sa.inspect(bind).get_table_names():
        settings = sa.table("settings", sa.column("key"), sa.column("value"))
        bind.execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output(
        "Customer identity downgrade retains additive schema, mappings and service associations; API disabled. "
        "Re-upgrade to 0074 is supported; arbitrary older binaries are not certified."
    )
