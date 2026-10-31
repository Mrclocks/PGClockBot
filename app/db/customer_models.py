"""Independent customer identities and immutable website reference mappings."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, event, func, inspect
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.db import Base


def new_customer_public_id() -> str:
    return f"cus_{uuid4()}"


def new_service_public_id() -> str:
    return f"svc_{uuid4()}"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("state IN ('active', 'blocked')", name="ck_customer_state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True, default=new_customer_public_id)
    state: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    legacy_bot_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_users.id", ondelete="SET NULL"), nullable=True, unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationCustomer(Base):
    __tablename__ = "integration_customers"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_customer_ref", name="uq_integration_customer_ref"),
        UniqueConstraint("integration_id", "customer_id", name="uq_integration_customer_identity"),
        CheckConstraint("length(external_customer_ref) BETWEEN 1 AND 128", name="ck_customer_reference_length"),
        CheckConstraint("length(display_name) <= 128", name="ck_customer_display_length"),
        Index("ix_integration_customers_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    external_customer_ref: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@event.listens_for(IntegrationCustomer, "before_update")
def reject_mapping_reassignment(mapper: Mapper, connection: Connection, target: IntegrationCustomer) -> None:
    if any(inspect(target).attrs[key].history.has_changes()
           for key in ("integration_id", "customer_id", "external_customer_ref")):
        raise ValueError("Customer mapping identity is immutable")


@event.listens_for(Customer, "before_update")
def reject_customer_reassignment(mapper: Mapper, connection: Connection, target: Customer) -> None:
    if any(inspect(target).attrs[key].history.has_changes() for key in ("public_id", "legacy_bot_user_id")):
        raise ValueError("Customer identity is immutable")
