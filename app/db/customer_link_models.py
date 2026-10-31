"""Dual-proof Telegram links and permanent scoped challenge replay barriers."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CustomerLinkChallenge(Base):
    __tablename__ = "customer_link_challenges"
    __table_args__ = (
        UniqueConstraint("integration_id", "creation_key_digest", name="uq_customer_link_creation_key"),
        Index("ix_customer_link_pending", "integration_id", "customer_id", "expires_at"),
        Index("ix_customer_link_recent", "integration_id", "customer_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: f"lnk_{uuid4()}")
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    initiating_credential_selector: Mapped[str] = mapped_column(String(32))
    creation_key_digest: Mapped[str] = mapped_column(String(64))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    candidate_bot_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    telegram_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CustomerTelegramLink(Base):
    __tablename__ = "customer_telegram_links"
    __table_args__ = (
        Index("uq_active_customer_link", "integration_id", "customer_id", unique=True,
              sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),
        Index("uq_active_integration_telegram_link", "integration_id", "bot_user_id", unique=True,
              sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),
        Index("ix_customer_telegram_links_bot_user_id", "bot_user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"))
    challenge_id: Mapped[str] = mapped_column(ForeignKey("customer_link_challenges.id"), unique=True)
    consent_version: Mapped[int] = mapped_column(default=1, server_default="1")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerLinkAction(Base):
    __tablename__ = "customer_link_actions"
    __table_args__ = (UniqueConstraint("integration_id", "key_digest", name="uq_customer_link_action_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey("integrations.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    key_digest: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    challenge_id: Mapped[str | None] = mapped_column(ForeignKey("customer_link_challenges.id"), nullable=True)
    credential_selector: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_bot_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
