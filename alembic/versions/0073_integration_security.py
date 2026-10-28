"""Add shared admission counters and bounded authentication/security evidence.

Populated downgrades retain security evidence/counters and disable API access.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073_integration_security"
down_revision = "0072_integration_admin_audit"
branch_labels = None
depends_on = None

TABLES = ("integration_rate_counters", "integration_security_audit")
INDEXES = (
    ("integration_rate_counters", "ix_integration_counter_retention", ["window", "subject", "budget"]),
    ("integration_security_audit", "ix_integration_security_retention", ["evidence_hold", "created_at", "id"]),
    ("integration_security_audit", "ix_integration_security_scope", ["owner_principal_id", "scope_kind", "shop_id", "integration_id", "id"]),
)
OUTCOMES = (
    "succeeded", "invalid_request", "unsupported_authentication", "invalid_credential", "api_disabled",
    "integration_disabled", "principal_inactive", "permission_denied", "authorization_unavailable",
    "rate_limited", "security_unavailable", "internal_error",
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if TABLES[0] not in tables:
        op.create_table(
            TABLES[0],
            sa.Column("subject", sa.String(80), primary_key=True),
            sa.Column("budget", sa.String(16), primary_key=True),
            sa.Column("window", sa.Integer(), primary_key=True),
            sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
            sa.CheckConstraint("used >= 0", name="ck_integration_counter_used"),
            sa.CheckConstraint("budget IN ('read', 'write', 'private', 'preauth', 'audit')", name="ck_integration_counter_budget"),
        )
    if TABLES[1] not in tables:
        op.create_table(
            TABLES[1],
            sa.Column("id", sa.String(80), primary_key=True),
            sa.Column("request_id", sa.String(40), nullable=False),
            sa.Column("event", sa.String(16), nullable=False, server_default="authentication"),
            sa.Column("outcome", sa.String(32), nullable=False),
            sa.Column("integration_id", sa.String(41), nullable=True),
            sa.Column("owner_principal_id", sa.Integer(), nullable=True),
            sa.Column("scope_kind", sa.String(16), nullable=True),
            sa.Column("shop_id", sa.Integer(), nullable=True),
            sa.Column("ip_digest", sa.String(64), nullable=True),
            sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("evidence_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.CheckConstraint("outcome IN (" + ",".join(repr(value) for value in OUTCOMES) + ")", name="ck_integration_security_outcome"),
            sa.CheckConstraint("event = 'authentication'", name="ck_integration_security_event"),
            sa.CheckConstraint("occurrences > 0", name="ck_integration_security_count"),
            sa.CheckConstraint(
                "(integration_id IS NULL AND owner_principal_id IS NULL AND scope_kind IS NULL AND shop_id IS NULL) OR "
                "(integration_id IS NOT NULL AND owner_principal_id IS NOT NULL AND scope_kind IS NOT NULL AND "
                "((scope_kind = 'platform' AND shop_id IS NULL) OR "
                "(scope_kind = 'shop' AND shop_id IS NOT NULL AND shop_id > 0)))",
                name="ck_integration_security_scope",
            ),
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
            "Integration security downgrade retains populated schema and evidence; API disabled. Re-upgrade is supported."
        )
        return
    for table in reversed(TABLES):
        if table in tables:
            op.drop_table(table)
