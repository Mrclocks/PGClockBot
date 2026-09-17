"""Mini App commerce extras — auto-renew, cart recovery, pause, traffic ETA.

Revision ID: 0028_miniapp_commerce_extras
Revises: 0027_bot_users_color_tag
Create Date: 2026-09-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_miniapp_commerce_extras"
down_revision: Union[str, None] = "0027_bot_users_color_tag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    svc_cols = {c["name"] for c in insp.get_columns("user_services")}
    if "auto_renew_enabled" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("auto_renew_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "auto_renew_plan_id" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("auto_renew_plan_id", sa.Integer(), nullable=True),
        )
    if "auto_renew_fail_count" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("auto_renew_fail_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "auto_renew_last_at" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("auto_renew_last_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "paused_at" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "pause_reason" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("pause_reason", sa.String(length=255), nullable=True),
        )
    if "quota_used_bytes" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("quota_used_bytes", sa.BigInteger(), nullable=True),
        )
    if "traffic_sample_at" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("traffic_sample_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "predicted_exhaust_at" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("predicted_exhaust_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "notified_traffic_predict" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column(
                "notified_traffic_predict",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "onboarding_sent_at" not in svc_cols:
        op.add_column(
            "user_services",
            sa.Column("onboarding_sent_at", sa.DateTime(timezone=True), nullable=True),
        )

    order_cols = {c["name"] for c in insp.get_columns("orders")}
    if "cart_reminded_at" not in order_cols:
        op.add_column(
            "orders",
            sa.Column("cart_reminded_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "cart_remind_count" not in order_cols:
        op.add_column(
            "orders",
            sa.Column("cart_remind_count", sa.Integer(), nullable=False, server_default="0"),
        )

    user_cols = {c["name"] for c in insp.get_columns("bot_users")}
    if "emergency_credit_debt" not in user_cols:
        op.add_column(
            "bot_users",
            sa.Column("emergency_credit_debt", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    user_cols = {c["name"] for c in insp.get_columns("bot_users")}
    if "emergency_credit_debt" in user_cols:
        op.drop_column("bot_users", "emergency_credit_debt")

    order_cols = {c["name"] for c in insp.get_columns("orders")}
    for col in ("cart_remind_count", "cart_reminded_at"):
        if col in order_cols:
            op.drop_column("orders", col)

    svc_cols = {c["name"] for c in insp.get_columns("user_services")}
    for col in (
        "onboarding_sent_at",
        "notified_traffic_predict",
        "predicted_exhaust_at",
        "traffic_sample_at",
        "quota_used_bytes",
        "pause_reason",
        "paused_at",
        "auto_renew_last_at",
        "auto_renew_fail_count",
        "auto_renew_plan_id",
        "auto_renew_enabled",
    ):
        if col in svc_cols:
            op.drop_column("user_services", col)
