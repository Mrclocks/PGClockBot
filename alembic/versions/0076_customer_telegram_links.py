"""Add retained dual-proof consent and scoped challenge replay barriers."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076_customer_telegram_links"
down_revision = "0075_service_channel_ownership"
branch_labels = None
depends_on = None


def _create_challenges() -> None:
    op.create_table("customer_link_challenges",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("initiating_credential_selector", sa.String(32), nullable=False),
        sa.Column("creation_key_digest", sa.String(64), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("candidate_bot_user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=True),
        sa.Column("telegram_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("integration_id", "creation_key_digest", name="uq_customer_link_creation_key"))


def _create_links() -> None:
    op.create_table("customer_telegram_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("bot_user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=False),
        sa.Column("challenge_id", sa.String(40), sa.ForeignKey("customer_link_challenges.id"), nullable=False, unique=True),
        sa.Column("consent_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))


def _create_actions() -> None:
    op.create_table("customer_link_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("integration_id", sa.String(41), sa.ForeignKey("integrations.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("challenge_id", sa.String(40), sa.ForeignKey("customer_link_challenges.id"), nullable=True),
        sa.Column("credential_selector", sa.String(32), nullable=True),
        sa.Column("actor_bot_user_id", sa.Integer(), sa.ForeignKey("bot_users.id"), nullable=True),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("integration_id", "key_digest", name="uq_customer_link_action_key"))


def upgrade() -> None:
    bind = op.get_bind()
    for name, create in (("customer_link_challenges", _create_challenges),
                         ("customer_telegram_links", _create_links), ("customer_link_actions", _create_actions)):
        if name not in sa.inspect(bind).get_table_names():
            create()
    for table, name, columns, unique in (
        ("customer_link_challenges", "ix_customer_link_pending", ["integration_id", "customer_id", "expires_at"], False),
        ("customer_link_challenges", "ix_customer_link_recent", ["integration_id", "customer_id", "created_at"], False),
        ("customer_telegram_links", "uq_active_customer_link", ["integration_id", "customer_id"], True),
        ("customer_telegram_links", "uq_active_integration_telegram_link", ["integration_id", "bot_user_id"], True),
        ("customer_telegram_links", "ix_customer_telegram_links_bot_user_id", ["bot_user_id", "id"], False),
    ):
        if name not in {index["name"] for index in sa.inspect(bind).get_indexes(table)}:
            partial = {"sqlite_where": sa.text("ended_at IS NULL"), "postgresql_where": sa.text("ended_at IS NULL")} if unique else {}
            op.create_index(name, table, columns, unique=unique, **partial)


def downgrade() -> None:
    settings = sa.table("settings", sa.column("key"), sa.column("value"))
    op.get_bind().execute(settings.update().where(settings.c.key == "integration_api_enabled").values(value="0"))
    op.get_context().impl.static_output("Telegram linking downgrade retains consent, challenges and replay evidence; API disabled.")
