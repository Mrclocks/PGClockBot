"""Retain encrypted private delivery and explicit integration pack grants."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079_integration_private_delivery"
down_revision = "0078_integration_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'integration_deliveries' not in tables:
        op.create_table('integration_deliveries',
            sa.Column('service_id', sa.Integer(), sa.ForeignKey('user_services.id'), primary_key=True, nullable=False),
            sa.Column('url_enc', sa.Text(), nullable=False),
            sa.Column('url_digest', sa.String(length=64), nullable=False),
            sa.Column('access_version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('validated_at', sa.DateTime(timezone=True), nullable=False))
    if 'integration_addon_grants' not in tables:
        op.create_table('integration_addon_grants',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), nullable=False),
            sa.Column('pack_id', sa.Integer(), sa.ForeignKey('service_addon_packs.id'), nullable=False),
            sa.Column('public_id', sa.String(length=40), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.UniqueConstraint('integration_id', 'pack_id', name='uq_integration_addon_pack'),
            sa.UniqueConstraint('public_id', name=None))
    if 'ix_integration_addon_grants_integration_id' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_addon_grants')}:
        op.create_index('ix_integration_addon_grants_integration_id', 'integration_addon_grants', ['integration_id'], unique=False)
    if 'ix_integration_addon_grants_pack_id' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_addon_grants')}:
        op.create_index('ix_integration_addon_grants_pack_id', 'integration_addon_grants', ['pack_id'], unique=False)


def downgrade() -> None:
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    op.get_bind().execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output("Private delivery downgrade retains encrypted material and pack handles; API disabled.")
