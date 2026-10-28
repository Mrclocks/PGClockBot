"""Persist technical intent, mutation fences and non-expiring capacity evidence."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078_integration_operations"
down_revision = "0077_integration_read_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'integration_operations' not in tables:
        op.create_table('integration_operations',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('public_id', sa.String(length=39), nullable=False),
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), nullable=False),
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=False),
            sa.Column('service_id', sa.Integer(), sa.ForeignKey('user_services.id'), nullable=True),
            sa.Column('accepting_credential_selector', sa.String(length=32), nullable=False),
            sa.Column('external_ref', sa.String(length=128), nullable=False),
            sa.Column('external_ref_digest', sa.String(length=64), nullable=False),
            sa.Column('input_digest', sa.String(length=64), nullable=False),
            sa.Column('input_json', sa.Text(), nullable=False),
            sa.Column('accepted_terms_json', sa.Text(), nullable=False),
            sa.Column('kind', sa.String(length=16), nullable=False),
            sa.Column('state', sa.String(length=24), nullable=False, server_default='queued'),
            sa.Column('resource_version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('target_json', sa.Text(), nullable=True),
            sa.Column('baseline_json', sa.Text(), nullable=True),
            sa.Column('lease_token', sa.String(length=64), nullable=True),
            sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('dispatch_intent_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('dispatch_sent_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('failure_code', sa.String(length=48), nullable=True),
            sa.Column('evidence_hold', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('public_id', name=None),
            sa.CheckConstraint('resource_version > 0', name='ck_integration_operation_version'),
            sa.CheckConstraint("kind IN ('provision','renew','addon')", name='ck_integration_operation_kind'),
            sa.UniqueConstraint('integration_id', 'external_ref_digest', name='uq_integration_external_intent'),
            sa.CheckConstraint("state IN ('queued','running','outcome_unknown','needs_review','succeeded','failed')", name='ck_integration_operation_state'))
    if 'ix_integration_operations_customer' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_operations')}:
        op.create_index('ix_integration_operations_customer', 'integration_operations', ['integration_id', 'customer_id', 'id'], unique=False)
    if 'ix_integration_operations_integration_state' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_operations')}:
        op.create_index('ix_integration_operations_integration_state', 'integration_operations', ['integration_id', 'state', 'id'], unique=False)
    if 'ix_integration_operations_lease' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_operations')}:
        op.create_index('ix_integration_operations_lease', 'integration_operations', ['state', 'lease_expires_at', 'id'], unique=False)
    if 'ix_integration_operations_queue' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_operations')}:
        op.create_index('ix_integration_operations_queue', 'integration_operations', ['state', 'id'], unique=False)
    if 'ix_integration_operations_service_id' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_operations')}:
        op.create_index('ix_integration_operations_service_id', 'integration_operations', ['service_id'], unique=False)
    if 'integration_operation_audit' not in tables:
        op.create_table('integration_operation_audit',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('operation_id', sa.Integer(), sa.ForeignKey('integration_operations.id'), nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('state', sa.String(length=24), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('actor_principal_id', sa.Integer(), sa.ForeignKey('org_principals.id'), nullable=True),
            sa.Column('code', sa.String(length=48), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('operation_id', 'kind', 'version', name='uq_operation_evidence_version'))
    if 'ix_integration_operation_audit_operation_id' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_operation_audit')}:
        op.create_index('ix_integration_operation_audit_operation_id', 'integration_operation_audit', ['operation_id'], unique=False)
    if 'integration_native_mutations' not in tables:
        op.create_table('integration_native_mutations',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('integration_id', sa.String(length=41), sa.ForeignKey('integrations.id'), nullable=False),
            sa.Column('service_id', sa.Integer(), sa.ForeignKey('user_services.id'), nullable=False),
            sa.Column('token', sa.String(length=64), nullable=False),
            sa.Column('state', sa.String(length=16), nullable=False, server_default='intent'),
            sa.Column('target_json', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('token', name=None))
    if 'ix_integration_native_review' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_native_mutations')}:
        op.create_index('ix_integration_native_review', 'integration_native_mutations', ['integration_id', 'state', 'id'], unique=False)
    if 'service_mutation_guards' not in tables:
        op.create_table('service_mutation_guards',
            sa.Column('service_id', sa.Integer(), sa.ForeignKey('user_services.id'), primary_key=True, nullable=False),
            sa.Column('operation_id', sa.Integer(), sa.ForeignKey('integration_operations.id'), nullable=True),
            sa.Column('native_mutation_id', sa.Integer(), sa.ForeignKey('integration_native_mutations.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint('(operation_id IS NOT NULL AND native_mutation_id IS NULL) OR (operation_id IS NULL AND native_mutation_id IS NOT NULL)', name='ck_service_guard_owner'),
            sa.UniqueConstraint('native_mutation_id', name=None),
            sa.UniqueConstraint('operation_id', name=None))
    if 'integration_capacity_commitments' not in tables:
        op.create_table('integration_capacity_commitments',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('operation_id', sa.Integer(), sa.ForeignKey('integration_operations.id'), nullable=True),
            sa.Column('native_mutation_id', sa.Integer(), sa.ForeignKey('integration_native_mutations.id'), nullable=True),
            sa.Column('shop_id', sa.Integer(), sa.ForeignKey('bot_users.id'), nullable=True),
            sa.Column('principal_id', sa.Integer(), sa.ForeignKey('org_principals.id'), nullable=False),
            sa.Column('pg_username', sa.String(length=128), nullable=False, server_default=''),
            sa.Column('bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('user_slots', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint('(operation_id IS NOT NULL AND native_mutation_id IS NULL) OR (operation_id IS NULL AND native_mutation_id IS NOT NULL)', name='ck_integration_capacity_owner'),
            sa.UniqueConstraint('operation_id', name=None),
            sa.UniqueConstraint('native_mutation_id', name=None),
            sa.CheckConstraint('bytes >= 0 AND user_slots >= 0 AND user_slots <= 1', name='ck_integration_capacity_amounts'))
    if 'ix_integration_capacity_commitments_pg_username' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_capacity_commitments')}:
        op.create_index('ix_integration_capacity_commitments_pg_username', 'integration_capacity_commitments', ['pg_username'], unique=False)
    if 'ix_integration_capacity_principal_active' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_capacity_commitments')}:
        op.create_index('ix_integration_capacity_principal_active', 'integration_capacity_commitments', ['principal_id', 'active'], unique=False)
    if 'ix_integration_capacity_shop_active' not in {index["name"] for index in sa.inspect(bind).get_indexes('integration_capacity_commitments')}:
        op.create_index('ix_integration_capacity_shop_active', 'integration_capacity_commitments', ['shop_id', 'active'], unique=False)


def downgrade() -> None:
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    op.get_bind().execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output("Operation downgrade retains targets, duplicate barriers, guards and all commitments; API disabled.")
