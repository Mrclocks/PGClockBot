"""Stable catalog handles and explicit credential-free API service observations."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IntegrationCatalogPlan(Base):
    __tablename__ = "integration_catalog_plans"
    __table_args__ = (UniqueConstraint("integration_id", "plan_id", name="uq_integration_catalog_plan"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: f"pln_{uuid4()}")
    terms_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationServiceSnapshot(Base):
    __tablename__ = "integration_service_snapshots"
    __table_args__ = (
        CheckConstraint("state IN ('provisioning','ready','review','cancelled')", name="ck_api_service_state"),
        CheckConstraint("resource_version > 0", name="ck_api_service_version"),
    )

    service_id: Mapped[int] = mapped_column(ForeignKey("user_services.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="provisioning", server_default="provisioning")
    resource_version: Mapped[int] = mapped_column(default=1, server_default="1")
    panel_status: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    limit_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    on_hold_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    volume_unlimited: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    time_unlimited: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    panel_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    panel_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationRequestReplay(Base):
    __tablename__ = "integration_request_replays"
    __table_args__ = (Index("ix_integration_request_replays_expiry", "expires_at"),)

    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), primary_key=True)
    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_digest: Mapped[str] = mapped_column(String(64))
    resource_kind: Mapped[str] = mapped_column(String(16))
    resource_id: Mapped[str] = mapped_column(String(41))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
