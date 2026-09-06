from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(str, Enum):
    USER = "user"
    RESELLER = "reseller"
    ADMIN = "admin"


class OrderStatus(str, Enum):
    PENDING = "pending"
    AWAITING_RECEIPT = "awaiting_receipt"
    AWAITING_APPROVAL = "awaiting_approval"
    PAID = "paid"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class PaymentMethod(str, Enum):
    WALLET = "wallet"
    CARD = "card"
    GATEWAY = "gateway"
    CRYPTO = "crypto"
    STARS = "stars"
    PSP = "psp"  # Iranian/online PSP with request→verify (additive)


class PaymentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TicketStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class PanelTicketStatus(str, Enum):
    """Internal panel tickets (reseller / pg_staff ↔ platform admin)."""

    OPEN = "open"  # منتظر پاسخ صاحب
    IN_PROGRESS = "in_progress"  # در حال بررسی
    ANSWERED = "answered"  # پاسخ داده شد
    CLOSED = "closed"


class PanelTicketPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class BotUser(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default=Role.USER.value, index=True)
    wallet_balance: Mapped[int] = mapped_column(Integer, default=0)
    points_balance: Mapped[int] = mapped_column(Integer, default=0)
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    referred_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True
    )
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    # Phase 1E — nullable; NULL ≠ global. Prefer over reseller_id when set.
    owner_principal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_principals.id"), nullable=True, index=True
    )
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    staff_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risk_flags: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # CSV
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    services: Mapped[list["UserService"]] = relationship(
        back_populates="owner", foreign_keys="UserService.bot_user_id"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="user", foreign_keys="Order.user_id"
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[int] = mapped_column(Integer)  # toman
    duration_days: Mapped[int] = mapped_column(Integer, default=30)
    data_limit_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pg_template_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pg_group_ids: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Optional per-plan username naming; NULL/empty → fall back to global settings
    pg_username_prefix: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pg_username_suffix: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pg_username_pattern: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # NULL = inherit plan-kind btn_style_*; "" = explicit Telegram default (white)
    button_style: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # NULL = platform (admin) catalog; set for reseller-owned shop plans
    owner_reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True, index=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    # Phase 1E — nullable; NULL ≠ global. Prefer over reseller_id when set.
    owner_principal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_principals.id"), nullable=True, index=True
    )
    amount: Mapped[int] = mapped_column(Integer)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default=OrderStatus.PENDING.value, index=True)
    payment_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    discount_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    service_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_services.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["BotUser"] = relationship(back_populates="orders", foreign_keys=[user_id])
    plan: Mapped[Optional["Plan"]] = relationship()
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(32), default=PaymentMethod.CARD.value)
    status: Mapped[str] = mapped_column(String(32), default=PaymentStatus.PENDING.value, index=True)
    receipt_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_wallet_topup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Optional["Order"]] = relationship(back_populates="payments")
    settlements: Mapped[list["PaymentSettlement"]] = relationship(back_populates="payment")


class SettlementStatus(str, Enum):
    """Lifecycle of an external settlement attempt (PSP / card-auto)."""

    CREATED = "created"
    AWAITING = "awaiting"
    SETTLING = "settling"  # claimed for approve; not terminal
    SETTLED = "settled"
    FAILED = "failed"
    EXPIRED = "expired"


class PaymentSettlement(Base):
    """Additive settlement ledger for PSP and card-auto channels.

    Existing receipt-based methods never touch this table. Settlement success
    always terminates in ``approve_payment`` (fail-closed, amount-matched).
    """

    __tablename__ = "payment_settlements"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payment_settlements_idempotency"),
        Index("ix_payment_settlements_external_ref", "provider", "external_ref"),
        # One open attempt per payment+channel (race-safe create).
        Index(
            "uq_payment_settlements_active_payment_channel",
            "payment_id",
            "channel",
            unique=True,
            sqlite_where=text("status IN ('created','awaiting','settling')"),
            postgresql_where=text("status IN ('created','awaiting','settling')"),
        ),
        # Duplicate provider events within a tenant.
        Index(
            "uq_payment_settlements_tenant_ext_ref",
            "channel",
            "provider",
            "tenant_key",
            "external_ref",
            unique=True,
            sqlite_where=text("external_ref IS NOT NULL AND status IN ('settling','settled')"),
            postgresql_where=text("external_ref IS NOT NULL AND status IN ('settling','settled')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    # NULL = platform shop; set for reseller tenant isolation on webhooks/matching.
    shop_owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    # 0 = platform; else reseller user id — for UNIQUE indexes (NULL-safe).
    tenant_key: Mapped[int] = mapped_column(Integer, default=0, index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)  # psp | card_auto
    provider: Mapped[str] = mapped_column(String(32), index=True)  # mock | zarinpal | generic
    status: Mapped[str] = mapped_column(
        String(32), default=SettlementStatus.CREATED.value, index=True
    )
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="IRT")
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # One-time token for mock checkout settle (never a substitute for real PSP verify).
    checkout_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    provider_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    payment: Mapped["Payment"] = relationship(back_populates="settlements")


class UserService(Base):
    __tablename__ = "user_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True)
    # Phase 1E — nullable; shop tenant via BotUser.reseller_id until set.
    owner_principal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_principals.id"), nullable=True, index=True
    )
    pg_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    pg_username: Mapped[str] = mapped_column(String(128))
    subscription_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subscription_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    remark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notified_expire: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_traffic: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cached live PG quota for users-list (modal reads PG; list must not fan-out).
    # When quota_synced_at is set: prefer these over plan/created_at approximations.
    # quota_expire_at None + synced ⇒ unlimited time; quota_data_limit_bytes 0 + synced ⇒ unlimited volume.
    quota_expire_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quota_data_limit_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    quota_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    renew_nudge_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped["BotUser"] = relationship(
        back_populates="services", foreign_keys=[bot_user_id]
    )
    plan: Mapped[Optional["Plan"]] = relationship()


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    # Shop that owns this ticket (None = platform main bot). Same pattern as Order.reseller_id.
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    # Phase 1E — nullable; NULL ≠ global. Prefer over reseller_id when set.
    owner_principal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_principals.id"), nullable=True, index=True
    )
    subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=TicketStatus.OPEN.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["TicketMessage"]] = relationship(back_populates="ticket")


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    sender_id: Mapped[int] = mapped_column(BigInteger)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["Ticket"] = relationship(back_populates="messages")


class PanelTicket(Base):
    """Web-panel support thread: reseller / pg_staff → platform admin (owner)."""

    __tablename__ = "panel_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default=PanelTicketStatus.OPEN.value, index=True
    )
    priority: Mapped[str] = mapped_column(
        String(32), default=PanelTicketPriority.NORMAL.value, index=True
    )
    # opener_role: "reseller" | "pg_staff"
    opener_role: Mapped[str] = mapped_column(String(32), index=True)
    opener_reseller_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    opener_pg_staff_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("pg_staff_access.id"), nullable=True, index=True
    )
    opener_label: Mapped[str] = mapped_column(String(128), default="")
    # True when owner replied and opener has not viewed since
    answered_unread: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # True when opener created/replied and owner has not viewed since
    owner_unread: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list["PanelTicketMessage"]] = relationship(
        back_populates="ticket", order_by="PanelTicketMessage.id"
    )


class PanelTicketMessage(Base):
    __tablename__ = "panel_ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("panel_tickets.id"), index=True)
    # sender_role: "admin" | "reseller" | "pg_staff"
    sender_role: Mapped[str] = mapped_column(String(32))
    sender_label: Mapped[str] = mapped_column(String(128), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    attachment_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    attachment_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attachment_mime: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ticket: Mapped["PanelTicket"] = relationship(back_populates="messages")


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BroadcastLog(Base):
    __tablename__ = "broadcast_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audience: Mapped[str] = mapped_column(String(32), default="all")
    text: Mapped[str] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResellerProfile(Base):
    __tablename__ = "reseller_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), unique=True)
    can_approve_receipts: Mapped[bool] = mapped_column(Boolean, default=False)
    pg_admin_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Fernet ciphertext of the PasarGuard admin password (shop ops must use this, not owner).
    pg_admin_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pg_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    share_pg_panel_url: Mapped[bool] = mapped_column(Boolean, default=False)
    web_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True)
    web_password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Unified feature permissions (web + bot must stay identical)
    web_permissions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # CSV
    bot_permissions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # CSV — mirrored
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reseller_plans.id"), nullable=True)
    plan: Mapped[Optional["ResellerPlan"]] = relationship()
    setup_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    setup_token_expires: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    setup_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    bot_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bot_telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    # Extra Telegram IDs that get reseller panel on THIS shop's dedicated bot only
    bot_admin_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # CSV
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- Unified Billing (PAYG vs fixed package type) ---
    billing_mode: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed | payg
    billing_balance: Mapped[int] = mapped_column(Integer, default=0)  # mirror of shop wallet (payg)
    billing_watermark_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    billing_low_warned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # PAYG empty-balance suspension (admin + users cut until topup)
    billing_suspended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    billing_suspended_user_ids: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # JSON list of PG user ids disabled by suspend
    # After True: shop wallet is SoT; billing_balance is kept as a mirror
    payg_wallet_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    capacity_warned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResellerBillingTransaction(Base):
    """Audit ledger for reseller PAYG (topup / usage / adjustment). Money lives on shop wallet."""

    __tablename__ = "reseller_billing_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_reseller_billing_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # topup | usage | adjustment
    amount: Mapped[int] = mapped_column(Integer)  # +credit / -debit (toman)
    balance_after: Mapped[int] = mapped_column(Integer)
    bytes_delta: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    rate_per_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    watermark_after: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(191))
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResellerBillingRate(Base):
    """Extensible price table. MVP UI uses global Setting; this enables per-reseller/plan/inbound/node later."""

    __tablename__ = "reseller_billing_rates"
    __table_args__ = (
        UniqueConstraint(
            "scope_kind",
            "scope_key",
            "reseller_user_id",
            name="uq_reseller_billing_rate_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # scope_kind: default | reseller | plan | inbound | node
    scope_kind: Mapped[str] = mapped_column(String(32), index=True, default="reseller")
    scope_key: Mapped[str] = mapped_column(String(128), default="")  # plan id / inbound / node id
    reseller_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    price_per_gb: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResellerSetting(Base):
    """Per-reseller shop bot settings (welcome, menus, payments, …)."""

    __tablename__ = "reseller_settings"
    __table_args__ = (UniqueConstraint("reseller_user_id", "key", name="uq_reseller_setting"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text, default="")


class ResellerPlan(Base):
    """Sellable reseller packages (price + default permissions / PG role)."""

    __tablename__ = "reseller_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[int] = mapped_column(Integer, default=0)  # toman; 0 = free apply
    can_approve_receipts: Mapped[bool] = mapped_column(Boolean, default=False)
    web_permissions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_permissions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    create_pg_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    create_web_access: Mapped[bool] = mapped_column(Boolean, default=True)
    share_pg_panel_url: Mapped[bool] = mapped_column(Boolean, default=False)
    pg_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Default billing for new resellers on this package: fixed | payg
    billing_mode: Mapped[str] = mapped_column(String(16), default="fixed")
    # PAYG: toman per GB for resellers on this package (overrides global Setting via plan rate)
    price_per_gb: Mapped[int] = mapped_column(Integer, default=0)
    # Optional PasarGuard group ids this PAYG price is intended for (comma-separated)
    pg_group_ids: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # subscription | addon_volume | addon_users
    plan_kind: Mapped[str] = mapped_column(String(32), default="subscription")
    # Subscription period in days; 0 = no time limit (also skip for PAYG)
    duration_days: Mapped[int] = mapped_column(Integer, default=0)
    included_gb: Mapped[int] = mapped_column(Integer, default=0)
    included_users: Mapped[int] = mapped_column(Integer, default=0)
    # Pack size for addon_volume / addon_users kinds
    addon_gb: Mapped[int] = mapped_column(Integer, default=0)
    addon_users: Mapped[int] = mapped_column(Integer, default=0)
    # fixed = renew_price/price; from_capacity = base + extras×unit
    renew_pricing_mode: Mapped[str] = mapped_column(String(32), default="fixed")
    # Reseller capacity add-ons (buy extra volume/users) + renew
    allow_buy_extra: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_gb_price: Mapped[int] = mapped_column(Integer, default=0)  # toman per extra GB
    extra_user_price: Mapped[int] = mapped_column(Integer, default=0)  # toman per extra user slot
    renew_price: Mapped[int] = mapped_column(Integer, default=0)  # 0 = use plan.price
    # NULL = inherit plan-kind btn_style_*; "" = explicit Telegram default (white)
    button_style: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PgAdminSubscription(Base):
    """Shared time+capacity clock for a PasarGuard admin (reseller and/or pg_staff)."""

    __tablename__ = "pg_admin_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pg_username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("reseller_plans.id"), nullable=True, index=True
    )
    # active | expired | revoked
    access_status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    base_gb: Mapped[int] = mapped_column(Integer, default=0)
    base_users: Mapped[int] = mapped_column(Integer, default=0)
    extra_gb_purchased: Mapped[int] = mapped_column(Integer, default=0)
    extra_users_purchased: Mapped[int] = mapped_column(Integer, default=0)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_disabled_user_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_renewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    renew_generation: Mapped[int] = mapped_column(Integer, default=0)
    warn_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    plan: Mapped[Optional["ResellerPlan"]] = relationship()


class ResellerApplicationStatus(str, Enum):
    PENDING_PAYMENT = "pending_payment"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ResellerApplication(Base):
    __tablename__ = "reseller_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("reseller_plans.id"), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=ResellerApplicationStatus.AWAITING_APPROVAL.value, index=True
    )
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    admin_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["BotUser"] = relationship(foreign_keys=[user_id])
    plan: Mapped["ResellerPlan"] = relationship()


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text, default="")


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PgStaffAccess(Base):
    """Web-panel login for an existing PasarGuard admin (owner-granted)."""

    __tablename__ = "pg_staff_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pg_username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    web_username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    web_password_hash: Mapped[str] = mapped_column(String(255))
    # Phase C5: encrypted PasarGuard password (same secret_box as resellers)
    pg_admin_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pg_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OrgPrincipal(Base):
    """Hierarchy SoT: Owner (depth 0) → depth-1 → depth-2. Not derived from role names."""

    __tablename__ = "org_principals"
    __table_args__ = (
        UniqueConstraint("reseller_profile_id", name="uq_org_principals_reseller_profile"),
        UniqueConstraint("pg_staff_id", name="uq_org_principals_pg_staff"),
        # At most one depth-0 / parent_id NULL row (active or disabled).
        Index(
            "uq_org_principals_single_owner",
            "depth",
            unique=True,
            sqlite_where=text("depth = 0 AND parent_id IS NULL"),
            postgresql_where=text("depth = 0 AND parent_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_principals.id"), nullable=True, index=True
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    pg_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    # Phase 2C: encrypted PasarGuard password for this Principal's own PG identity
    # (never Owner env credentials). Used for tenant-safe PG client selection.
    pg_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reseller_profile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("reseller_profiles.id"), nullable=True
    )
    pg_staff_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("pg_staff_access.id"), nullable=True
    )
    bot_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OrgPrincipalProvision(Base):
    """Idempotency ledger for Level-1 Principal provisioning (Phase 2A).

    Does not store Owner PG credentials. Records completed provisions so retries
    return the same Principal instead of minting duplicates.
    """

    __tablename__ = "org_principal_provisions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_org_principal_provision_idem"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_id: Mapped[int] = mapped_column(
        ForeignKey("org_principals.id"), nullable=False, index=True
    )
    pg_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    pg_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_by_principal_id: Mapped[int] = mapped_column(
        ForeignKey("org_principals.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrgPrincipalWebIdentity(Base):
    """Independent Web login for a Level-1 OrgPrincipal (Phase 2B).

    Resolves server-side to ``principal_id``. Does not store PG passwords or
    Owner credentials. Cookie sessions may carry ``web_identity_id`` only as a
    lookup key — hierarchy fields are never trusted from the client.
    """

    __tablename__ = "org_principal_web_identities"
    __table_args__ = (
        UniqueConstraint("principal_id", name="uq_org_principal_web_identity_principal"),
        UniqueConstraint("web_username", name="uq_org_principal_web_identity_username"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[int] = mapped_column(
        ForeignKey("org_principals.id"), nullable=False, index=True
    )
    web_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    web_password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TrialClaim(Base):
    """One free trial per user per shop — unique constraint closes the race window."""

    __tablename__ = "trial_claims"
    __table_args__ = (UniqueConstraint("user_id", "shop_key", name="uq_trial_claims_user_shop"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    shop_key: Mapped[str] = mapped_column(String(64))  # "platform" or str(reseller_id)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PointsTransaction(Base):
    """Auditable loyalty points ledger (source of truth; BotUser.points_balance is cache)."""

    __tablename__ = "points_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_points_tx_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)  # signed: +earn / -spend
    balance_after: Mapped[int] = mapped_column(Integer)
    tx_type: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(64), default="system", index=True)
    reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    meta_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reversed_tx_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("points_transactions.id"), nullable=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PointsRule(Base):
    """Configurable earn rules evaluated on loyalty events."""

    __tablename__ = "points_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    # fixed | per_gb
    amount_mode: Mapped[str] = mapped_column(String(32), default="fixed")
    amount: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_time_only: Mapped[bool] = mapped_column(Boolean, default=False)
    min_purchase_toman: Mapped[int] = mapped_column(Integer, default=0)
    min_purchase_gb: Mapped[int] = mapped_column(Integer, default=0)
    max_reward: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=0)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True)
    tier_min_points: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LoyaltyReward(Base):
    """Redeemable catalog items priced in points."""

    __tablename__ = "loyalty_rewards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # traffic_gb | time_days | wallet_credit | discount_percent
    reward_type: Mapped[str] = mapped_column(String(32), index=True)
    reward_value: Mapped[int] = mapped_column(Integer)  # GB / days / toman / percent
    points_cost: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    max_redemptions_global: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_redemptions_per_user: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    redemption_count: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    # Discount-only constraints (ignored for other reward types)
    min_purchase_toman: Mapped[int] = mapped_column(Integer, default=0)
    max_discount_toman: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expires_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RewardRedemption(Base):
    __tablename__ = "reward_redemptions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_reward_redemption_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    reward_id: Mapped[int] = mapped_column(ForeignKey("loyalty_rewards.id"), index=True)
    points_spent: Mapped[int] = mapped_column(Integer)
    reward_type: Mapped[str] = mapped_column(String(32))
    reward_value: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    service_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_services.id"), nullable=True
    )
    points_tx_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("points_transactions.id"), nullable=True
    )
    wallet_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    discount_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    meta_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoyaltyDiscountEntitlement(Base):
    """Personal one-time loyalty discount usable via existing checkout discount step."""

    __tablename__ = "loyalty_discount_entitlements"
    __table_args__ = (
        UniqueConstraint("code", name="uq_loyalty_discount_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    # Nullable when issued by lucky wheel (no reward redemption row).
    redemption_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("reward_redemptions.id"), nullable=True, index=True
    )
    wheel_spin_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("lucky_wheel_spins.id"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(64), index=True)
    percent: Mapped[int] = mapped_column(Integer)
    min_purchase_toman: Mapped[int] = mapped_column(Integer, default=0)
    max_discount_toman: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # available | reserved | consumed | void
    status: Mapped[str] = mapped_column(String(32), default="available", index=True)
    reserved_order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), nullable=True, index=True
    )
    consumed_order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), nullable=True
    )
    original_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    discount_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    final_amount: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reserved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReferralEvent(Base):
    """Referral lifecycle events (signup / qualification / purchase). Level-1 only."""

    __tablename__ = "referral_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_referral_event_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    referred_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(64), default="telegram")
    status: Mapped[str] = mapped_column(String(32), default="recorded")
    qualification_state: Mapped[str] = mapped_column(String(32), default="pending")
    reward_state: Mapped[str] = mapped_column(String(32), default="none")
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    meta_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoyaltyTier(Base):
    __tablename__ = "loyalty_tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    min_points: Mapped[int] = mapped_column(Integer, default=0)
    max_points: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    multiplier_bps: Mapped[int] = mapped_column(Integer, default=10000)  # 10000 = 1.0x
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class DeliveryFailure(Base):
    """Orders that failed Telegram/PG delivery and need retry."""

    __tablename__ = "delivery_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payments.id"), nullable=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChargeCode(Base):
    """Wallet gift / charge codes (fixed toman credit)."""

    __tablename__ = "charge_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)  # toman
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PanelInboxDismissal(Base):
    """Per-staff snooze/hide for computed inbox alerts (not a notification store)."""

    __tablename__ = "panel_inbox_dismissals"
    __table_args__ = (
        UniqueConstraint(
            "staff_key",
            "alert_key",
            "entity_id",
            name="uq_panel_inbox_dismiss_staff_alert",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staff_key: Mapped[str] = mapped_column(String(128), index=True)
    alert_key: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(16))  # 24h | forever
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FunnelEvent(Base):
    """Lightweight purchase-funnel analytics."""

    __tablename__ = "funnel_events"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_funnel_events_idem"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bot_users.id"), nullable=True, index=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    step: Mapped[str] = mapped_column(String(64), index=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LuckyWheelPrize(Base):
    """Weighted prize segment for the shop lucky wheel."""

    __tablename__ = "lucky_wheel_prizes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    # none | points | traffic_gb | time_days | wallet_credit | discount_percent | free_spin
    prize_type: Mapped[str] = mapped_column(String(32), index=True)
    prize_value: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    max_wins_global: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_wins_per_user: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    win_count: Mapped[int] = mapped_column(Integer, default=0)
    # Discount-only constraints (ignored for other types)
    min_purchase_toman: Mapped[int] = mapped_column(Integer, default=0)
    max_discount_toman: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expires_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LuckyWheelSpin(Base):
    """Immutable spin ledger — server-side outcome only."""

    __tablename__ = "lucky_wheel_spins"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_lucky_wheel_spin_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    prize_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("lucky_wheel_prizes.id"), nullable=True, index=True
    )
    prize_type: Mapped[str] = mapped_column(String(32), default="none")
    prize_value: Mapped[int] = mapped_column(Integer, default=0)
    prize_label_snapshot: Mapped[str] = mapped_column(String(128), default="")
    cost_points: Mapped[int] = mapped_column(Integer, default=0)
    used_free_spin: Mapped[bool] = mapped_column(Boolean, default=False)
    # completed | failed
    status: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    points_tx_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("points_transactions.id"), nullable=True
    )
    service_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_services.id"), nullable=True
    )
    discount_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    meta_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LuckyWheelUserState(Base):
    """Per-user wheel counters for cooldown / daily / free-spin UX."""

    __tablename__ = "lucky_wheel_user_state"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_lucky_wheel_user_state_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    free_spins_balance: Mapped[int] = mapped_column(Integer, default=0)
    last_spin_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    spins_today: Mapped[int] = mapped_column(Integer, default=0)
    spins_day: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD UTC
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )



class TermsAcceptance(Base):
    """Per-user acceptance of a terms gate (entry / purchase), scoped per shop."""

    __tablename__ = "terms_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "bot_user_id",
            "shop_owner_id",
            "gate",
            name="uq_terms_acceptances_user_shop_gate",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    shop_owner_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 0 = platform
    gate: Mapped[str] = mapped_column(String(32))  # entry | buy_user | buy_reseller
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
