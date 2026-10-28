"""Shared admission counters and bounded, secret-free authentication evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SECURITY_OUTCOMES = (
    "succeeded", "invalid_request", "unsupported_authentication", "invalid_credential", "api_disabled",
    "integration_disabled", "principal_inactive", "permission_denied", "authorization_unavailable",
    "rate_limited", "security_unavailable", "internal_error",
)
OUTCOME_CHECK = "outcome IN (" + ",".join(repr(value) for value in SECURITY_OUTCOMES) + ")"


class IntegrationRateCounter(Base):
    __tablename__ = "integration_rate_counters"
    __table_args__ = (
        CheckConstraint("used >= 0", name="ck_integration_counter_used"),
        CheckConstraint("budget IN ('read', 'write', 'private', 'preauth', 'audit')", name="ck_integration_counter_budget"),
        Index("ix_integration_counter_retention", "window", "subject", "budget"),
    )

    subject: Mapped[str] = mapped_column(String(80), primary_key=True)
    budget: Mapped[str] = mapped_column(String(16), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, server_default="0")


class IntegrationSecurityAudit(Base):
    __tablename__ = "integration_security_audit"
    __table_args__ = (
        CheckConstraint(OUTCOME_CHECK, name="ck_integration_security_outcome"),
        CheckConstraint("event = 'authentication'", name="ck_integration_security_event"),
        CheckConstraint("occurrences > 0", name="ck_integration_security_count"),
        CheckConstraint(
            "(integration_id IS NULL AND owner_principal_id IS NULL AND scope_kind IS NULL AND shop_id IS NULL) OR "
            "(integration_id IS NOT NULL AND owner_principal_id IS NOT NULL AND scope_kind IS NOT NULL AND "
            "((scope_kind = 'platform' AND shop_id IS NULL) OR (scope_kind = 'shop' AND shop_id IS NOT NULL AND shop_id > 0)))",
            name="ck_integration_security_scope",
        ),
        Index("ix_integration_security_retention", "evidence_hold", "created_at", "id"),
        Index("ix_integration_security_scope", "owner_principal_id", "scope_kind", "shop_id", "integration_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(40))
    event: Mapped[str] = mapped_column(String(16), server_default="authentication")
    outcome: Mapped[str] = mapped_column(String(32))
    # Historical attribution deliberately survives retained-schema downgrades.
    integration_id: Mapped[str | None] = mapped_column(String(41), nullable=True)
    owner_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    shop_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    evidence_hold: Mapped[bool] = mapped_column(Boolean, server_default=false())
