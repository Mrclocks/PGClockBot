"""Idempotent Mini App addon preview keys on orders.

Revision ID: 0089_addon_request_key
Revises: 0088_customer_services_permission

Adds ``addon_request_key`` so a lost Mini App response can safely retry the same
volume/time purchase without creating a second order (mirrors renewal_request_key).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0089_addon_request_key"
down_revision: Union[str, None] = "0088_customer_services_permission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "orders" not in tables:
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns("orders")}
    if "addon_request_key" not in columns:
        op.add_column("orders", sa.Column("addon_request_key", sa.String(64), nullable=True))
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("orders")}
    if "uq_orders_addon_request_key" not in indexes:
        op.create_index(
            "uq_orders_addon_request_key", "orders", ["addon_request_key"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "orders" not in tables:
        return
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("orders")}
    if "uq_orders_addon_request_key" in indexes:
        op.drop_index("uq_orders_addon_request_key", table_name="orders")
    columns = {c["name"] for c in sa.inspect(bind).get_columns("orders")}
    if "addon_request_key" in columns:
        op.drop_column("orders", "addon_request_key")
