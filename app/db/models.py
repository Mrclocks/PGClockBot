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
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    referred_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True
    )
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True
    )
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
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


class UserService(Base):
    __tablename__ = "user_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True)
    pg_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    pg_username: Mapped[str] = mapped_column(String(128))
    subscription_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subscription_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    remark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notified_expire: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_traffic: Mapped[bool] = mapped_column(Boolean, default=False)
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
    commission_percent: Mapped[int] = mapped_column(Integer, default=10)
    balance: Mapped[int] = mapped_column(Integer, default=0)
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
    setup_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    setup_token_expires: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    setup_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    bot_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bot_telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    # Extra Telegram IDs that get reseller panel on THIS shop's dedicated bot only
    bot_admin_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # CSV
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- Unified Billing (PAYG is one mode; fixed = legacy commission, untouched) ---
    billing_mode: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed | payg
    billing_balance: Mapped[int] = mapped_column(Integer, default=0)  # prepaid toman (payg)
    billing_watermark_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    billing_low_warned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResellerBillingTransaction(Base):
    """Audit ledger for reseller Billing (topup / usage / adjustment). Independent of user wallet."""

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
    commission_percent: Mapped[int] = mapped_column(Integer, default=10)
    can_approve_receipts: Mapped[bool] = mapped_column(Boolean, default=False)
    web_permissions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_permissions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    create_pg_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    create_web_access: Mapped[bool] = mapped_column(Boolean, default=True)
    share_pg_panel_url: Mapped[bool] = mapped_column(Boolean, default=False)
    pg_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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
    # Encrypted PasarGuard admin password — required for staff-scoped API calls.
    # Never fall back to the platform Owner token when this is missing.
    pg_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
