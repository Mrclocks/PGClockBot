"""Secret-free management replay barriers and transactional administrative history."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SCOPE_CHECK = (
    "(scope_kind = 'platform' AND shop_id IS NULL) OR "
    "(scope_kind = 'shop' AND shop_id IS NOT NULL AND shop_id > 0)"
)
ACTION_CHECK = "action IN ('create', 'configure', 'plans', 'issue', 'rotate', 'revoke')"


class IntegrationManagementAction(Base):
    __tablename__ = "integration_management_actions"
    __table_args__ = (
        CheckConstraint(SCOPE_CHECK, name="ck_integration_action_scope"),
        CheckConstraint(ACTION_CHECK, name="ck_integration_action_type"),
        Index("ix_integration_actions_integration", "integration_id"),
        Index("ix_integration_actions_actor", "actor_principal_id"),
        Index("ix_integration_actions_shop", "shop_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    actor_principal_id: Mapped[int] = mapped_column(ForeignKey("org_principals.id"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    # Historical targets survive a retained-schema downgrade with no live integrations.
    integration_id: Mapped[str | None] = mapped_column(String(41), nullable=True)
    action: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntegrationAdminAudit(Base):
    __tablename__ = "integration_admin_audit"
    __table_args__ = (
        CheckConstraint(SCOPE_CHECK, name="ck_integration_audit_scope"),
        CheckConstraint(ACTION_CHECK, name="ck_integration_audit_type"),
        CheckConstraint("outcome IN ('succeeded', 'denied')", name="ck_integration_audit_outcome"),
        Index("ix_integration_audit_owner_page", "actor_principal_id", "scope_kind", "shop_id", "id"),
        Index("ix_integration_audit_scope_page", "actor_principal_id", "scope_kind", "shop_id", "integration_id", "id"),
        Index("ix_integration_audit_integration", "integration_id"),
        Index("ix_integration_audit_shop", "shop_id"),
        Index("ix_integration_audit_retention", "evidence_hold", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("integration_management_actions.id"), unique=True)
    actor_principal_id: Mapped[int] = mapped_column(ForeignKey("org_principals.id"))
    scope_kind: Mapped[str] = mapped_column(String(16))
    shop_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    # Historical targets survive a retained-schema downgrade with no live integrations.
    integration_id: Mapped[str | None] = mapped_column(String(41), nullable=True)
    action: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    credential_selector: Mapped[str | None] = mapped_column(String(32), nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    after: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    evidence_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
