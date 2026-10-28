"""Add disabled integration identities, allowed plans and hashed credentials.

Populated downgrades retain the entire foundation and disable API access. This
does not certify compatibility with arbitrary earlier application binaries.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0071_integration_foundation"
down_revision = "0070_shop_miniapp_key"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)
TABLES = ("integrations", "integration_plans", "integration_credentials")
INDEXES = (
    ("integrations", "ix_integrations_owner_principal_id", ["owner_principal_id"]),
    ("integrations", "ix_integrations_shop_id", ["shop_id"]),
    ("integration_plans", "ix_integration_plans_plan_id", ["plan_id"]),
    ("integration_credentials", "ix_integration_credentials_integration_id", ["integration_id"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "integrations" not in tables:
        op.create_table(
            "integrations",
            sa.Column("id", sa.String(41), primary_key=True),
            sa.Column("owner_principal_id", sa.Integer(), sa.ForeignKey("org_principals.id"), nullable=False),
            sa.Column("scope_kind", sa.String(16), nullable=False),
            sa.Column("shop_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="disabled"),
            sa.Column("grants", sa.Text(), nullable=False, server_default=""),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("status IN ('active', 'disabled', 'retired')", name="ck_integration_status"),
            sa.CheckConstraint(
                "(scope_kind = 'platform' AND shop_id IS NULL) OR "
                "(scope_kind = 'shop' AND shop_id IS NOT NULL AND shop_id > 0)",
                name="ck_integration_scope",
            ),
        )
    if "integration_plans" not in tables:
        op.create_table(
            "integration_plans",
            sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), primary_key=True),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id"), primary_key=True),
        )
    if "integration_credentials" not in tables:
        op.create_table(
            "integration_credentials",
            sa.Column("selector", sa.String(32), primary_key=True),
            sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=False),
            sa.Column("secret_digest", sa.String(64), nullable=False),
            sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_integration_credential_status"),
            sa.CheckConstraint("expires_at > created_at", name="ck_integration_credential_expiry"),
        )
    for table, name, columns in INDEXES:
        existing = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)


def _has_retained_references(bind: sa.Connection, tables: set[str]) -> bool:
    inspector = sa.inspect(bind)
    foundation_tables = set(TABLES)
    return any(
        foreign_key["referred_table"] in foundation_tables
        for table in tables - foundation_tables
        for foreign_key in inspector.get_foreign_keys(table)
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "settings" in tables:
        settings = sa.table("settings", sa.column("key"), sa.column("value"))
        bind.execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    populated = any(
        bind.scalar(sa.select(sa.literal(1)).select_from(sa.table(table)).limit(1)) is not None
        for table in TABLES if table in tables
    )
    if populated or _has_retained_references(bind, tables):
        notice = (
            "Integration foundation downgrade retains populated schema or retained foreign-key dependencies; "
            "API disabled. Re-upgrade is supported."
        )
        logger.warning(notice)
        # Alembic's logging reconfiguration can disable a previously loaded revision logger.
        op.get_context().impl.static_output(notice)
        return
    for table in reversed(TABLES):
        if table in tables:
            op.drop_table(table)
