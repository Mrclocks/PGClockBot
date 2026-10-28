"""Durable technical intent, execution fencing, capacity commitments and native evidence."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IntegrationOperation(Base):
    __tablename__ = "integration_operations"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_ref_digest", name="uq_integration_external_intent"),
        Index("ix_integration_operations_queue", "state", "id"),
        Index("ix_integration_operations_integration_state", "integration_id", "state", "id"),
        Index("ix_integration_operations_customer", "integration_id", "customer_id", "id"),
        Index("ix_integration_operations_lease", "state", "lease_expires_at", "id"),
        CheckConstraint("kind IN ('provision','renew','addon')", name="ck_integration_operation_kind"),
        CheckConstraint("state IN ('queued','running','outcome_unknown','needs_review','succeeded','failed')", name="ck_integration_operation_state"),
        CheckConstraint("resource_version > 0", name="ck_integration_operation_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(39), unique=True, default=lambda: f"op_{uuid4()}")
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("user_services.id"), nullable=True, index=True)
    accepting_credential_selector: Mapped[str] = mapped_column(String(32))
    external_ref: Mapped[str] = mapped_column(String(128))
    external_ref_digest: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[str] = mapped_column(Text)
    accepted_terms_json: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24), default="queued", server_default="queued")
    resource_version: Mapped[int] = mapped_column(default=1, server_default="1")
    target_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    evidence_hold: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationOperationAudit(Base):
    __tablename__ = "integration_operation_audit"
    __table_args__ = (UniqueConstraint("operation_id", "kind", "version", name="uq_operation_evidence_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("integration_operations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column()
    actor_principal_id: Mapped[int | None] = mapped_column(ForeignKey("org_principals.id"), nullable=True)
    code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationNativeMutation(Base):
    __tablename__ = "integration_native_mutations"
    __table_args__ = (Index("ix_integration_native_review", "integration_id", "state", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("user_services.id"))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="intent", server_default="intent")
    target_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceMutationGuard(Base):
    __tablename__ = "service_mutation_guards"
    __table_args__ = (CheckConstraint(
        "(operation_id IS NOT NULL AND native_mutation_id IS NULL) OR (operation_id IS NULL AND native_mutation_id IS NOT NULL)",
        name="ck_service_guard_owner"),)

    service_id: Mapped[int] = mapped_column(ForeignKey("user_services.id"), primary_key=True)
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("integration_operations.id"), nullable=True, unique=True)
    native_mutation_id: Mapped[int | None] = mapped_column(ForeignKey("integration_native_mutations.id"), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationCapacityCommitment(Base):
    __tablename__ = "integration_capacity_commitments"
    __table_args__ = (
        Index("ix_integration_capacity_shop_active", "shop_id", "active"),
        Index("ix_integration_capacity_principal_active", "principal_id", "active"),
        CheckConstraint("bytes >= 0 AND user_slots >= 0 AND user_slots <= 1", name="ck_integration_capacity_amounts"),
        CheckConstraint("(operation_id IS NOT NULL AND native_mutation_id IS NULL) OR (operation_id IS NULL AND native_mutation_id IS NOT NULL)",
                        name="ck_integration_capacity_owner"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int | None] = mapped_column(ForeignKey("integration_operations.id"), nullable=True, unique=True)
    native_mutation_id: Mapped[int | None] = mapped_column(ForeignKey("integration_native_mutations.id"), nullable=True, unique=True)
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("org_principals.id"))
    pg_username: Mapped[str] = mapped_column(String(128), default="", server_default="", index=True)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    user_slots: Mapped[int] = mapped_column(default=0, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
