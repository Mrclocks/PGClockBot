"""Explicit, retaining integration add-on allowlist with stable public handles."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IntegrationAddonGrant(Base):
    __tablename__ = "integration_addon_grants"
    __table_args__ = (UniqueConstraint("integration_id", "pack_id", name="uq_integration_addon_pack"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"), index=True)
    pack_id: Mapped[int] = mapped_column(ForeignKey("service_addon_packs.id"), index=True)
    public_id: Mapped[str] = mapped_column(String(40), unique=True, default=lambda: f"pack_{uuid4().hex}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
