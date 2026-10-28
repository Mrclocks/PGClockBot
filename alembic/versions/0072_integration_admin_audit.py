"""Add transactional management audits and durable form replay barriers.

Populated downgrades retain administrative evidence and disable public API access.
Retained schema does not establish compatibility with older application binaries.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072_integration_admin_audit"
down_revision = "0071_integration_foundation"
branch_labels = None
depends_on = None

TABLES = ("integration_management_actions", "integration_admin_audit")
SCOPE_CHECK = (
    "(scope_kind = 'platform' AND shop_id IS NULL) OR "
    "(scope_kind = 'shop' AND shop_id IS NOT NULL AND shop_id > 0)"
)
ACTION_CHECK = "action IN ('create', 'configure', 'plans', 'issue', 'rotate', 'revoke')"
INDEXES = (
    ("integrations", "ix_integrations_owner_scope_id", ["owner_principal_id", "scope_kind", "shop_id", "id"]),
    ("integration_credentials", "ix_integration_credentials_integration_selector", ["integration_id", "selector"]),
    ("integration_management_actions", "ix_integration_actions_integration", ["integration_id"]),
    ("integration_management_actions", "ix_integration_actions_actor", ["actor_principal_id"]),
    ("integration_management_actions", "ix_integration_actions_shop", ["shop_id"]),
    ("integration_admin_audit", "ix_integration_audit_owner_page", ["actor_principal_id", "scope_kind", "shop_id", "id"]),
    ("integration_admin_audit", "ix_integration_audit_scope_page", ["actor_principal_id", "scope_kind", "shop_id", "integration_id", "id"]),
    ("integration_admin_audit", "ix_integration_audit_integration", ["integration_id"]),
    ("integration_admin_audit", "ix_integration_audit_shop", ["shop_id"]),
    ("integration_admin_audit", "ix_integration_audit_retention", ["evidence_hold", "created_at", "id"]),
)


def _authority_columns() -> list[sa.Column]:
    return [
        sa.Column("actor_principal_id", sa.Integer(), sa.ForeignKey("org_principals.id"), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("shop_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=True),
        sa.Column("integration_id", sa.String(41), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "integration_management_actions" not in tables:
        op.create_table(
            "integration_management_actions", sa.Column("id", sa.String(32), primary_key=True), *_authority_columns(),
            sa.CheckConstraint(SCOPE_CHECK, name="ck_integration_action_scope"),
            sa.CheckConstraint(ACTION_CHECK, name="ck_integration_action_type"),
        )
    if "integration_admin_audit" not in tables:
        op.create_table(
            "integration_admin_audit", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("action_id", sa.String(32), sa.ForeignKey("integration_management_actions.id"), nullable=False, unique=True),
            *_authority_columns(),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("error_code", sa.String(32), nullable=True),
            sa.Column("credential_selector", sa.String(32), nullable=True),
            sa.Column("before", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("after", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("evidence_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.CheckConstraint(SCOPE_CHECK, name="ck_integration_audit_scope"),
            sa.CheckConstraint(ACTION_CHECK, name="ck_integration_audit_type"),
            sa.CheckConstraint("outcome IN ('succeeded', 'denied')", name="ck_integration_audit_outcome"),
        )
    for table, name, columns in INDEXES:
        if name not in {index["name"] for index in sa.inspect(bind).get_indexes(table)}:
            op.create_index(name, table, columns)


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
    if populated:
        op.get_context().impl.static_output(
            "Integration administration downgrade retains populated schema and replay barriers; API disabled. Re-upgrade is supported."
        )
        return
    for table in reversed(TABLES):
        if table in tables:
            op.drop_table(table)
    for table, name, _ in INDEXES[:2]:
        if table in tables and name in {index["name"] for index in sa.inspect(bind).get_indexes(table)}:
            op.drop_index(name, table_name=table)
