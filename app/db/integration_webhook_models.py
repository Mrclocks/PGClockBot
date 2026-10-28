"""Durable event identity, pinned destination versions and private signing material."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IntegrationWebhookDestination(Base):
    __tablename__ = "integration_webhook_destinations"
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationWebhookKey(Base):
    __tablename__ = "integration_webhook_keys"
    __table_args__ = (Index("ix_webhook_keys_current", "integration_id", "created_at", "id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: f"whk_{uuid4().hex}")
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    secret_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        UniqueConstraint("integration_id", "type", "resource_id", "resource_version", name="uq_integration_event_version"),
        Index("ix_integration_events_delivery", "state", "next_attempt_at", "id"),
        Index("ix_integration_events_scope", "integration_id", "id"),
        CheckConstraint("state IN ('pending','delivering','acknowledged','exhausted')", name="ck_integration_event_state"),
        CheckConstraint("attempts >= 0 AND attempts <= 13", name="ck_integration_event_attempts"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: f"evt_{uuid4().hex}")
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(40))
    customer_id: Mapped[str] = mapped_column(String(40))
    resource_version: Mapped[int] = mapped_column()
    body: Mapped[str] = mapped_column(Text)
    destination_version: Mapped[int | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[int | None] = mapped_column(nullable=True)
    last_failure: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationEventAttempt(Base):
    __tablename__ = "integration_event_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid4().hex)
    event_id: Mapped[int] = mapped_column(ForeignKey("integration_events.id"), index=True)
    key_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[int | None] = mapped_column(nullable=True)
    failure: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationLinkVersion(Base):
    __tablename__ = "integration_link_versions"
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), primary_key=True)
    version: Mapped[int] = mapped_column(default=1, server_default="1")


class IntegrationControlAction(Base):
    __tablename__ = "integration_control_actions"
    __table_args__ = (Index("ix_integration_control_scope", "integration_id", "id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    actor_principal_id: Mapped[int] = mapped_column(ForeignKey("org_principals.id"))
    kind: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
