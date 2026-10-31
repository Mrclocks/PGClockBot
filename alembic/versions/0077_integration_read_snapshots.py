"""Add stable catalog mappings and credential-free quota/freshness observations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077_integration_read_snapshots"
down_revision = "0076_customer_telegram_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "integration_catalog_plans" not in tables:
        op.create_table("integration_catalog_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=False),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), nullable=False),
            sa.Column("public_id", sa.String(40), nullable=False, unique=True),
            sa.Column("terms_json", sa.Text(), nullable=True),
            sa.Column("terms_revision", sa.String(64), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("integration_id", "plan_id", name="uq_integration_catalog_plan"))
    if "integration_service_snapshots" not in tables:
        op.create_table("integration_service_snapshots",
            sa.Column("service_id", sa.Integer(), sa.ForeignKey("user_services.id"), primary_key=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="provisioning"),
            sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("panel_status", sa.String(16), nullable=False, server_default="unknown"),
            sa.Column("limit_bytes", sa.BigInteger(), nullable=True),
            sa.Column("used_bytes", sa.BigInteger(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("on_hold_seconds", sa.BigInteger(), nullable=True),
            sa.Column("volume_unlimited", sa.Boolean(), nullable=True),
            sa.Column("time_unlimited", sa.Boolean(), nullable=True),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("panel_available", sa.Boolean(), nullable=True),
            sa.Column("panel_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("state IN ('provisioning','ready','review','cancelled')", name="ck_api_service_state"),
            sa.CheckConstraint("resource_version > 0", name="ck_api_service_version"))
    if "integration_request_replays" not in tables:
        op.create_table("integration_request_replays",
            sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), primary_key=True),
            sa.Column("key_digest", sa.String(64), primary_key=True),
            sa.Column("input_digest", sa.String(64), nullable=False),
            sa.Column("resource_kind", sa.String(16), nullable=False),
            sa.Column("resource_id", sa.String(41), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if "ix_integration_request_replays_expiry" not in {index["name"] for index in sa.inspect(bind).get_indexes("integration_request_replays")}:
        op.create_index("ix_integration_request_replays_expiry", "integration_request_replays", ["expires_at"])
    for name, column in (("ix_integration_catalog_plans_integration_id", "integration_id"),
                         ("ix_integration_catalog_plans_plan_id", "plan_id")):
        if name not in {index["name"] for index in sa.inspect(bind).get_indexes("integration_catalog_plans")}:
            op.create_index(name, "integration_catalog_plans", [column])


def downgrade() -> None:
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    op.get_bind().execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output("Read API downgrade retains catalog mappings and service observations; API disabled.")
