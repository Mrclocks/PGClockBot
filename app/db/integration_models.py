"""Integration identities, fixed shop boundaries and non-recoverable API keys."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, event, func, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.db import Base


class Integration(Base):
    __tablename__ = "integrations"
    __table_args__ = (
        Index("ix_integrations_owner_scope_id", "owner_principal_id", "scope_kind", "shop_id", "id"),
        CheckConstraint("customer_entry_policy IN ('denied', 'website')", name="ck_integration_customer_entry"),
        CheckConstraint("status IN ('active', 'disabled', 'retired')", name="ck_integration_status"),
        CheckConstraint(
            "(scope_kind = 'platform' AND shop_id IS NULL) OR (scope_kind = 'shop' AND shop_id IS NOT NULL AND shop_id > 0)",
            name="ck_integration_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(41), primary_key=True)
    owner_principal_id: Mapped[int] = mapped_column(ForeignKey("org_principals.id"), index=True)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="disabled", default="disabled")
    customer_entry_policy: Mapped[str] = mapped_column(String(16), default="denied", server_default="denied")
    grants: Mapped[str] = mapped_column(Text, server_default="", default="")
    revision: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationPlan(Base):
    __tablename__ = "integration_plans"

    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), primary_key=True, index=True)


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"
    __table_args__ = (
        Index("ix_integration_credentials_integration_selector", "integration_id", "selector"),
        CheckConstraint("status IN ('active', 'revoked')", name="ck_integration_credential_status"),
        CheckConstraint("expires_at > created_at", name="ck_integration_credential_expiry"),
    )

    selector: Mapped[str] = mapped_column(String(32), primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), index=True)
    secret_digest: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[str] = mapped_column(Text, server_default="", default="")
    status: Mapped[str] = mapped_column(String(16), server_default="active", default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # A rotated key can only be shortened or revoked, never rotated a second time.
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(Integration, "before_update")
def reject_integration_reassignment(mapper: Mapper, connection: Connection, target: Integration) -> None:
    state = inspect(target)
    if any(state.attrs[key].history.has_changes() for key in ("id", "owner_principal_id", "scope_kind", "shop_id")):
        raise ValueError("Integration ownership and shop are immutable")


@event.listens_for(IntegrationCredential, "before_update")
def reject_credential_reassignment(mapper: Mapper, connection: Connection, target: IntegrationCredential) -> None:
    state = inspect(target)
    if any(state.attrs[key].history.has_changes() for key in ("selector", "integration_id", "secret_digest")):
        raise ValueError("Credential identity is immutable")
