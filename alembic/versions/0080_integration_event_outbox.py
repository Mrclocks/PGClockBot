"""Retain event identities, signed webhook delivery evidence and operator actions."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0080_integration_event_outbox"
down_revision = "0079_integration_private_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'integration_webhook_destinations' not in tables:
        op.create_table('integration_webhook_destinations',
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), primary_key=True, nullable=False),
            sa.Column('url', sa.String(length=2048), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if 'integration_webhook_keys' not in tables:
        op.create_table('integration_webhook_keys',
            sa.Column('id', sa.String(length=40), primary_key=True, nullable=False),
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), nullable=False),
            sa.Column('secret_enc', sa.Text(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if 'ix_webhook_keys_current' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_webhook_keys')}:
        op.create_index('ix_webhook_keys_current', 'integration_webhook_keys', ['integration_id', 'created_at', 'id'], unique=False)
    if 'integration_events' not in tables:
        op.create_table('integration_events',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('event_id', sa.String(length=40), nullable=False),
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), nullable=False),
            sa.Column('type', sa.String(length=40), nullable=False),
            sa.Column('resource_id', sa.String(length=40), nullable=False),
            sa.Column('customer_id', sa.String(length=40), nullable=False),
            sa.Column('resource_version', sa.Integer(), nullable=False),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('destination_version', sa.Integer(), nullable=True),
            sa.Column('state', sa.String(length=16), nullable=False, server_default='pending'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('lease_token', sa.String(length=64), nullable=True),
            sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_status', sa.Integer(), nullable=True),
            sa.Column('last_failure', sa.String(length=32), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('integration_id', 'type', 'resource_id', 'resource_version', name='uq_integration_event_version'),
            sa.UniqueConstraint('event_id', name=None),
            sa.CheckConstraint("state IN ('pending','delivering','acknowledged','exhausted')", name='ck_integration_event_state'),
            sa.CheckConstraint('attempts >= 0 AND attempts <= 13', name='ck_integration_event_attempts'))
    if 'ix_integration_events_delivery' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_events')}:
        op.create_index('ix_integration_events_delivery', 'integration_events', ['state', 'next_attempt_at', 'id'], unique=False)
    if 'ix_integration_events_scope' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_events')}:
        op.create_index('ix_integration_events_scope', 'integration_events', ['integration_id', 'id'], unique=False)
    if 'integration_event_attempts' not in tables:
        op.create_table('integration_event_attempts',
            sa.Column('id', sa.String(length=32), primary_key=True, nullable=False),
            sa.Column('event_id', sa.Integer(), sa.ForeignKey('integration_events.id'), nullable=False),
            sa.Column('key_id', sa.String(length=40), nullable=True),
            sa.Column('status', sa.Integer(), nullable=True),
            sa.Column('failure', sa.String(length=32), nullable=True),
            sa.Column('latency_ms', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if 'ix_integration_event_attempts_event_id' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_event_attempts')}:
        op.create_index('ix_integration_event_attempts_event_id', 'integration_event_attempts', ['event_id'], unique=False)
    if 'integration_link_versions' not in tables:
        op.create_table('integration_link_versions',
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), primary_key=True, nullable=False),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), primary_key=True, nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
    if 'integration_control_actions' not in tables:
        op.create_table('integration_control_actions',
            sa.Column('id', sa.String(length=32), primary_key=True, nullable=False),
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), nullable=False),
            sa.Column('actor_principal_id', sa.Integer(), sa.ForeignKey('org_principals.id'), nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('resource_id', sa.String(length=40), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if 'ix_integration_control_scope' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_control_actions')}:
        op.create_index('ix_integration_control_scope', 'integration_control_actions', ['integration_id', 'id'], unique=False)


def downgrade() -> None:
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    op.get_bind().execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output("Event downgrade retains destinations, signing keys, attempts, events and operator evidence; API disabled.")
