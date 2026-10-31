from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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

import app.db.integration_models  # noqa: F401 — register foundation tables with shared metadata
import app.db.integration_security_models  # noqa: F401 — register shared security tables
from app.db.integration_read_models import (
    IntegrationCatalogPlan as IntegrationCatalogPlan,
    IntegrationRequestReplay as IntegrationRequestReplay,
    IntegrationServiceSnapshot as IntegrationServiceSnapshot,
)
from app.db import Base
from app.db.customer_link_models import (
    CustomerLinkAction as CustomerLinkAction,
)
from app.db.customer_link_models import (
    CustomerLinkChallenge as CustomerLinkChallenge,
)
from app.db.customer_link_models import (
    CustomerTelegramLink as CustomerTelegramLink,
)
from app.db.customer_models import (
    Customer as Customer,
)
from app.db.customer_models import (
    IntegrationCustomer as IntegrationCustomer,
)
from app.db.customer_models import (
    new_service_public_id,
)
from app.db.integration_audit_models import IntegrationAdminAudit as IntegrationAdminAudit
from app.db.integration_audit_models import IntegrationManagementAction as IntegrationManagementAction


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
    BBC = "bbc"  # BezanBeCard card-to-card match (per-shop API key)


class PaymentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    VOIDED = "voided"


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
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), index=True)
    staff_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Staff color marker (fixed palette in app.services.color_tags).
    color_tag: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    risk_flags: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # CSV
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    services: Mapped[list["UserService"]] = relationship(
        back_populates="owner", foreign_keys="UserService.bot_user_id"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="user", foreign_keys="Order.user_id"
    )


class PlanCategory(Base):
    """Shop-scoped grouping label for plans — selectable when adding a plan.

    ``audience``: ``users`` (sales Plan) or ``resellers`` (ResellerPlan packages).
    Not a replacement for plan kinds (fixed/trial/custom/…) — additive label.
    """

    __tablename__ = "plan_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # users | resellers — which unified-modal audience can pick this label
    audience: Mapped[str] = mapped_column(String(16), default="users", index=True)
    # NULL = platform (admin) catalog; set for reseller-owned shop categories
    owner_reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # NULL = inherit shop_kind_fixed; "" = Telegram default white; else primary/success/danger
    button_style: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Telegram Bot API icon_custom_emoji_id (premium). Name stays plain display text.
    icon_custom_emoji_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[int] = mapped_column(Integer)  # toman
    # 0 = unlimited duration (no expire on custom PG create / renew)
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
    # Optional shop category (fixed plans); NULL = uncategorized
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("plan_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # NULL = platform (admin) catalog; set for reseller-owned shop plans
    owner_reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceAddonPack(Base):
    """Customer add-on packs: extra volume (GB) or duration (days) on an existing service.

    ``is_general`` packs apply to every plan category of the shop. Otherwise
    ``category_id`` ties the pack to the plan category of the service's sales
    plan, so VIP and economy packs can be priced differently. A pack that is
    neither general nor tied to a category matches nothing.
    """

    __tablename__ = "service_addon_packs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # volume = GB; duration = days
    kind: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Float)  # GB or days
    price: Mapped[int] = mapped_column(Integer)  # toman
    # NULL = platform (admin) catalog; set for reseller-owned shop packs
    owner_reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("plan_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_general: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("uq_orders_renewal_request_key", "renewal_request_key", unique=True),
        Index("ix_orders_created_at", "created_at"),
        Index(
            "uq_orders_service_quota_mutation", "service_id", unique=True,
            sqlite_where=text("service_mutation_pending = true"),
            postgresql_where=text("service_mutation_pending = true"),
        ),
    )

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
    # Purchased terms and an absolute target, persisted before panel mutation.
    renewal_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    renewal_request_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Serialize quota changes and retain exclusion while a renewal needs review.
    service_mutation_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
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
    # Shop scope for wallet top-ups (None = platform / main-bot purse).
    # Set at top-up creation from the bot context — never inferred from sticky
    # user.reseller_id at settlement time (that allowed cross-tenant minting).
    wallet_shop_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Optional["Order"]] = relationship(back_populates="payments")
    settlements: Mapped[list["PaymentSettlement"]] = relationship(back_populates="payment")


class PaymentReviewMessage(Base):
    """Every staff copy of a payment review card, including photo captions."""

    __tablename__ = "payment_review_messages"

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    is_photo: Mapped[bool] = mapped_column(Boolean, default=False)


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
    channel: Mapped[str] = mapped_column(String(32), index=True)  # psp | card_auto | bbc
    provider: Mapped[str] = mapped_column(String(32), index=True)  # mock | zarinpal | sunpay | generic | bezanbecard
    # PSP channel: which of the shop's API gateways (``psp_gateways`` setting) issued the checkout;
    # NULL = created before several gateways existed (the single configured gateway).
    gateway_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
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
    # Set by the one caller that sends the delivery of a settled payment (claim, then send).
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the PSP poller may re-verify this row next; NULL = not checked yet (due now).
    next_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    payment: Mapped["Payment"] = relationship(back_populates="settlements")


class UserService(Base):
    __tablename__ = "user_services"
    __table_args__ = (
        Index("ix_user_services_pg_username_lower", text("lower(pg_username)")),
        Index("ix_user_services_shop_customer_id", "shop_id", "customer_id", "id"),
        Index("ix_user_services_integration_customer_id", "integration_id", "customer_id", "id"),
        CheckConstraint(
            "(bot_user_id IS NOT NULL AND integration_id IS NULL AND origin_operation_id IS NULL) OR "
            "(bot_user_id IS NULL AND customer_id IS NOT NULL AND public_id IS NOT NULL AND "
            "integration_id IS NOT NULL AND origin_operation_id IS NOT NULL AND "
            "owner_principal_id IS NOT NULL AND NOT shop_needs_review AND "
            "subscription_url IS NULL AND subscription_token IS NULL)",
            name="ck_service_channel_owner",
        ),
        # A panel user can be imported into «سرویس‌های من» by one customer only.
        Index(
            "uq_user_services_imported_pg_user", "pg_user_id", unique=True,
            sqlite_where=text("remark = 'imported'"),
            postgresql_where=text("remark = 'imported'"),
        ),
        # New services waiting for the first-use check; the check stamps the row, so it leaves the index.
        Index(
            "ix_user_services_first_use_pending", "created_at",
            sqlite_where=text("first_use_checked_at IS NULL"),
            postgresql_where=text("first_use_checked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True, index=True)
    integration_id: Mapped[str | None] = mapped_column(ForeignKey("integrations.id"), nullable=True, index=True)
    origin_operation_id: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    public_id: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True, default=new_service_public_id)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id"), nullable=True)
    # Phase 1E — nullable; shop tenant via BotUser.reseller_id until set.
    owner_principal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("org_principals.id"), nullable=True, index=True
    )
    pg_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    pg_username: Mapped[str] = mapped_column(String(128))
    cancellation_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
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
    quota_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    renew_nudge_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the first-use check ran for this service, whether or not a message was sent.
    first_use_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Shop that sold the service (NULL = platform), written when the service is created.
    # A service whose shop could not be derived when this column was introduced keeps
    # shop_needs_review and is hidden from every shop until the owner assigns it.
    shop_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    shop_needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped["BotUser | None"] = relationship(
        back_populates="services", foreign_keys=[bot_user_id]
    )
    plan: Mapped[Optional["Plan"]] = relationship()


class BotUserShop(Base):
    """A person is a customer of a shop (shop_id NULL = the platform).

    One person can buy from several shops; BotUser.reseller_id only records the
    shop that attributed them first. Rows are written idempotently on first contact
    with a bot and whenever an order or a service is created.
    """

    __tablename__ = "bot_user_shops"
    __table_args__ = (
        Index(
            "uq_bot_user_shops_shop", "bot_user_id", "shop_id", unique=True,
            sqlite_where=text("shop_id IS NOT NULL"),
            postgresql_where=text("shop_id IS NOT NULL"),
        ),
        Index(
            "uq_bot_user_shops_platform", "bot_user_id", unique=True,
            sqlite_where=text("shop_id IS NULL"),
            postgresql_where=text("shop_id IS NULL"),
        ),
        Index("ix_bot_user_shops_shop_user", "shop_id", "bot_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"))
    shop_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceImportAttempt(Base):
    """One try to add an existing service by its subscription link (rate limit and audit).

    Only a hash of the token is kept; the link itself is a bearer secret.
    """

    __tablename__ = "service_import_attempts"
    __table_args__ = (Index("ix_service_import_attempts_user_time", "bot_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))
    # Operator-facing cause of a refusal (service_import.Reason); never shown to customers.
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceImportRequest(Base):
    """A customer's request to add an existing service while admin approval is on.

    The subscription token is never stored: approval re-reads the panel user and derives the
    link from the panel's own record.
    """

    __tablename__ = "service_import_requests"
    __table_args__ = (
        Index(
            "uq_service_import_requests_open",
            "bot_user_id",
            "panel_user_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_service_import_requests_shop_status", "shop_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"))
    # Owning shop (None = the platform's own customers); fixed when the request is made.
    shop_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    panel_user_id: Mapped[int] = mapped_column(Integer)
    panel_username: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PanelAlertState(Base):
    """Last state of a panel alert that is pushed to Telegram, so each change is announced once.

    ``scope_key`` is ``platform`` or ``shop:<id>``; ``alert`` is the kind of alert.
    """

    __tablename__ = "panel_alert_states"
    __table_args__ = (Index("uq_panel_alert_states_scope_alert", "scope_key", "alert", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(32))
    alert: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VolumeHold(Base):
    """Volume a shop has promised but not yet recorded on a service, while a panel call runs.

    Counted in the shop's allocation until released or expired, so two concurrent sales cannot
    both fit into the same free volume. A hold whose panel outcome is unknown simply expires;
    the service row or the panel sync counts the volume from then on.
    """

    __tablename__ = "volume_holds"
    __table_args__ = (Index("ix_volume_holds_shop_expires", "shop_id", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer)
    hold_key: Mapped[str] = mapped_column(String(96), unique=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BotChatMessage(Base):
    """A message the bot may delete later to keep a private chat tidy (see ``app.bot.chat_cleanup``).

    Kept in the database so a restart does not forget what is still on screen. Telegram lets a
    bot delete a message only for 48 hours, so older rows are useless and purged.
    """

    __tablename__ = "bot_chat_messages"
    __table_args__ = (
        UniqueConstraint("bot_id", "chat_id", "message_id", name="uq_bot_chat_messages_message"),
        Index("ix_bot_chat_messages_sent_at", "sent_at"),
        Index("ix_bot_chat_messages_replace", "bot_id", "replace_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    # "throwaway" (panels, prompts, notices, taps), "home" (a message carrying the main menu) or
    # "notice" (a system alert that the next alert with the same ``replace_key`` replaces).
    kind: Mapped[str] = mapped_column(String(16))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    replace_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ServiceAutomation(Base):
    """Per-service wallet consent; deleted catalog choices become NULL."""

    __tablename__ = "service_automations"

    service_id: Mapped[int] = mapped_column(
        ForeignKey("user_services.id", ondelete="CASCADE"), primary_key=True
    )
    shop_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    renew_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    renew_plan_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
    )
    duration_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    duration_pack_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("service_addon_packs.id", ondelete="SET NULL"), nullable=True
    )
    volume_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    volume_pack_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("service_addon_packs.id", ondelete="SET NULL"), nullable=True
    )
    renew_notice: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    duration_notice: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    volume_notice: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Persist the order with the debit so a restart cannot buy the same cycle again.
    pending_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    pending_action: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    lock_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
    # Durable send state: rows written before the queue existed keep status "done".
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="done", index=True)
    cursor_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The user a message is being sent to; left set after a crash, the outcome is then unknown.
    inflight_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lock_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ResellerProfile(Base):
    __tablename__ = "reseller_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), unique=True)
    can_approve_receipts: Mapped[bool] = mapped_column(Boolean, default=False)
    pg_admin_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Fernet ciphertext of the PasarGuard admin password (shop ops must use this, not owner).
    pg_admin_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Fernet ciphertext of a PasarGuard admin API key (X-Api-Key). Preferred over password when set.
    pg_api_key_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    # SHA-256 hex of plaintext token for unique lookup without storing/searching ciphertext
    bot_token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    bot_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bot_telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    # Extra Telegram IDs that get reseller panel on THIS shop's dedicated bot only
    bot_admin_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # CSV
    # Random public key in this shop's Mini App address; the shop bot's token still signs every request.
    miniapp_key: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- Unified Billing (PAYG vs fixed package type) ---
    billing_mode: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed | payg
    billing_balance: Mapped[int] = mapped_column(Integer, default=0)  # mirror of shop wallet (payg)
    billing_watermark_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    billing_low_warned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Deepest low-balance level already announced (toman); NULL while above every level.
    billing_low_warned_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Last billing problem announced to the owner (see billing_alerts.TickIssue); NULL when healthy.
    billing_tick_issue: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
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
    # When False, allocated UserService volume cannot exceed PgAdminSubscription ceiling.
    # Migration default True keeps existing shops open; new profiles set False in code.
    allow_volume_oversell: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("true")
    )
    # Volume of PasarGuard users not present in UserService (refreshed by sync job).
    external_allocated_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    external_alloc_synced_at: Mapped[Optional[datetime]] = mapped_column(
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
    # subscription | current_capacity | addon_volume | addon_users
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
    # Optional category label (audience=resellers); NULL = uncategorized
    category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("plan_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Platform seed plans (e.g. «ظرفیت فعلی») — not operator-deletable / not sellable
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # None = platform / main-bot purse; set = per-shop wallet ledger row.
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShopWallet(Base):
    """Per-shop customer purse — isolated from platform ``BotUser.wallet_balance``.

    Credits minted inside a reseller shop (gift codes, loyalty, referral, top-up)
    land here and can only be spent on that shop's orders. PAYG / main-bot money
    stays on ``BotUser.wallet_balance``.
    """

    __tablename__ = "shop_wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "reseller_id", name="uq_shop_wallets_user_reseller"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PgStaffAccess(Base):
    """Web-panel login for an existing PasarGuard admin (owner-granted)."""

    __tablename__ = "pg_staff_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pg_username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    web_username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    web_password_hash: Mapped[str] = mapped_column(String(255))
    # Phase C5: encrypted PasarGuard password (same secret_box as resellers)
    pg_admin_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Fernet ciphertext of a PasarGuard admin API key (X-Api-Key). Preferred over password when set.
    pg_api_key_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    # Fernet ciphertext of a PasarGuard admin API key (X-Api-Key). Preferred over password when set.
    pg_api_key_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    """One free trial per user per shop — unique constraint closes the race window.

    Optional phone_hash (HMAC-SHA256, never raw) and telegram_id close contact-gated
    abuse windows. Uniques are shop-scoped so platform and reseller shops stay isolated.
    """

    __tablename__ = "trial_claims"
    __table_args__ = (
        UniqueConstraint("user_id", "shop_key", name="uq_trial_claims_user_shop"),
        UniqueConstraint("telegram_id", "shop_key", name="uq_trial_claims_tg_shop"),
        UniqueConstraint("phone_hash", "shop_key", name="uq_trial_claims_phone_shop"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"), index=True)
    shop_key: Mapped[str] = mapped_column(String(64))  # "platform" or str(reseller_id)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseContact(Base):
    """A customer's verified Telegram contact, isolated to the current shop."""

    __tablename__ = "purchase_contacts"

    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True)
    shop_key: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    phone_hash: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChargeCodeRedemption(Base):
    """Per-user gift-code redemption — UNIQUE(code_id, user_id) enforces one redeem each."""

    __tablename__ = "charge_code_redemptions"
    __table_args__ = (
        UniqueConstraint("code_id", "user_id", name="uq_charge_code_redemptions_code_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_id: Mapped[int] = mapped_column(
        ForeignKey("charge_codes.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentReceiptFingerprint(Base):
    """Telegram file_unique_id + image sha256 for duplicate-receipt review warnings."""

    __tablename__ = "payment_receipt_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    file_unique_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
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
    # Shop whose balance the entry moved (NULL = the platform's BotUser.points_balance).
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShopPoints(Base):
    """A customer's loyalty points in one shop; the platform's stay on ``BotUser.points_balance``."""

    __tablename__ = "shop_points"
    __table_args__ = (
        UniqueConstraint("user_id", "reseller_id", name="uq_shop_points_user_reseller"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    reseller_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)


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
    # Shop that issued the discount (None = platform). Must match order.reseller_id.
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
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
    """Shop-scoped wallet gifts and checkout discount codes."""

    __tablename__ = "charge_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)  # toman
    kind: Mapped[str] = mapped_column(String(16), default="wallet", server_default="wallet")
    percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_discount_toman: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses_per_user: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    first_purchase_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    purchase_types: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id"), nullable=True, index=True
    )
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChargeCodeUse(Base):
    """Reservations count towards limits until an unpaid order is released."""

    __tablename__ = "charge_code_uses"
    __table_args__ = (Index("ix_charge_code_uses_code_user_status", "charge_code_id", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    charge_code_id: Mapped[int] = mapped_column(ForeignKey("charge_codes.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id"))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="reserved")
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
    seen_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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
    """Wheel counters (cooldown, daily limit, free spins) of one customer in one shop's wheel."""

    __tablename__ = "lucky_wheel_user_state"
    __table_args__ = (
        Index(
            "uq_lucky_wheel_state_shop", "user_id", "reseller_id", unique=True,
            sqlite_where=text("reseller_id IS NOT NULL"),
            postgresql_where=text("reseller_id IS NOT NULL"),
        ),
        Index(
            "uq_lucky_wheel_state_platform", "user_id", unique=True,
            sqlite_where=text("reseller_id IS NULL"),
            postgresql_where=text("reseller_id IS NULL"),
        ),
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


class PaymentVoid(Base):
    """Audit and retry state of one approved payment that an operator voided.

    ``service_ids`` is the target saved before the panel is touched, so a retry after an
    unknown outcome disables exactly the same services.
    """

    __tablename__ = "payment_voids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), unique=True
    )
    shop_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    refund_amount: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    service_ids: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"))
    lock_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceCancellation(Base):
    __tablename__ = "service_cancellations"
    __table_args__ = (Index("ix_service_cancellations_notification", "notification_status", "id"), Index(
        "uq_service_cancellations_open", "service_id", unique=True,
        sqlite_where=text("status IN ('pending','processing','review','approved')"),
        postgresql_where=text("status IN ('pending','processing','review','approved')"),
    ),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("user_services.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    pg_user_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    refund_amount: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    operator_note: Mapped[str] = mapped_column(Text, default="")
    processed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lock_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    notification_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TargetedCampaign(Base):
    __tablename__ = "targeted_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reseller_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=True, index=True)
    audience: Mapped[str] = mapped_column(String(24))
    days: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(128))
    lock_token: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id", name="uq_campaign_recipient"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("targeted_campaigns.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketingPreference(Base):
    __tablename__ = "marketing_preferences"

    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True)
    shop_key: Mapped[int] = mapped_column(Integer, primary_key=True)  # 0 = platform
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class CatalogDiscount(Base):
    """Shop price adjustments, with a deduplicated announcement campaign."""

    __tablename__ = "catalog_discounts"
    __table_args__ = (
        UniqueConstraint("shop_key", "request_key", name="uq_catalog_discount_request"),
        UniqueConstraint("shop_key", "active_slot", name="uq_catalog_discount_slot"),
        Index("ix_catalog_discounts_shop_active", "shop_key", "is_active", "ends_at"),
        Index("ix_catalog_discounts_shop_history", "shop_key", "id"),
        Index("ix_catalog_discounts_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_key: Mapped[int] = mapped_column(Integer)
    reseller_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    active_slot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(16))
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("plan_categories.id", ondelete="SET NULL"), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    reduction_kind: Mapped[str] = mapped_column(String(16))
    reduction_value: Mapped[int] = mapped_column(Integer)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    request_key: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("targeted_campaigns.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConnectionMonitorState(Base):
    """Scoped account configuration and the shared local probe lease."""

    __tablename__ = "connection_monitor_states"
    __table_args__ = (
        UniqueConstraint("reseller_id", "probe_id", name="uq_monitor_state_shop_probe"),
        Index("uq_monitor_state_main_probe", "probe_id", unique=True,
              sqlite_where=text("reseller_id IS NULL"), postgresql_where=text("reseller_id IS NULL")),
        Index("ix_monitor_state_lease", "probe_id", "cycle_lease_until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    probe_id: Mapped[str] = mapped_column(String(16), server_default="local")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pg_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pg_username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subscription_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_revision: Mapped[str] = mapped_column(String(64), server_default="")
    cycle_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cycle_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    catalog_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectionMonitorTarget(Base):
    """A selected subscription config with private-by-default publication settings."""

    __tablename__ = "connection_monitor_targets"
    __table_args__ = (
        UniqueConstraint("reseller_id", "pg_host_id", name="uq_monitor_target_shop_host"),
        Index("uq_monitor_target_shop_config", "reseller_id", "subscription_key", unique=True),
        Index("uq_monitor_target_main_config", "subscription_key", unique=True,
              sqlite_where=text("reseller_id IS NULL AND subscription_key IS NOT NULL"),
              postgresql_where=text("reseller_id IS NULL AND subscription_key IS NOT NULL")),
        Index("uq_monitor_target_main_host", "pg_host_id", unique=True,
              sqlite_where=text("reseller_id IS NULL"), postgresql_where=text("reseller_id IS NULL")),
        Index("ix_monitor_target_due", "reseller_id", "test_enabled", "next_check_at", "id"),
        Index("ix_monitor_target_public", "reseller_id", "test_enabled", "is_public", "display_order", "id"),
        Index("ix_monitor_target_lease", "reseller_id", "claim_until"),
        CheckConstraint("display_order >= 0 AND display_order <= 2147483647", name="ck_monitor_target_order"),
        CheckConstraint("endpoint_count >= 0 AND endpoint_count <= 12", name="ck_monitor_target_endpoints"),
        CheckConstraint("NOT is_public OR (test_enabled AND length(trim(public_name)) > 0)", name="ck_monitor_target_public"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    pg_host_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscription_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_name: Mapped[str] = mapped_column(String(128), server_default="")
    test_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_public: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    public_name: Mapped[str] = mapped_column(String(64), server_default="")
    display_order: Mapped[int] = mapped_column(Integer, server_default="0")
    generation: Mapped[str] = mapped_column(String(64), server_default="")
    endpoint_count: Mapped[int] = mapped_column(Integer, server_default="0")
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_pending: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class ConnectionMonitorSnapshot(Base):
    """Bounded latest endpoint evidence; raw configs and credentials never belong here."""

    __tablename__ = "connection_monitor_snapshots"
    __table_args__ = (
        UniqueConstraint("target_id", "endpoint_key", "probe_id", name="uq_monitor_snapshot_endpoint"),
        Index("ix_monitor_snapshot_scope_target", "reseller_id", "target_id", "probe_id"),
        CheckConstraint("delay_ms IS NULL OR delay_ms >= 0", name="ck_monitor_snapshot_delay"),
        CheckConstraint("success_streak >= 0 AND failure_streak >= 0", name="ck_monitor_snapshot_streaks"),
        CheckConstraint("status IN ('healthy', 'degraded', 'down', 'unknown', 'not_ready', 'paused')", name="ck_monitor_snapshot_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reseller_id: Mapped[int | None] = mapped_column(ForeignKey("bot_users.id"), nullable=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("connection_monitor_targets.id", ondelete="CASCADE"))
    endpoint_key: Mapped[str] = mapped_column(String(64))
    probe_id: Mapped[str] = mapped_column(String(16), server_default="local")
    generation: Mapped[str] = mapped_column(String(64), server_default="")
    status: Mapped[str] = mapped_column(String(16), server_default="unknown")
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delay_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_streak: Mapped[int] = mapped_column(Integer, server_default="0")
    failure_streak: Mapped[int] = mapped_column(Integer, server_default="0")
    has_observation: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    recovery_required: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    samples_json: Mapped[str] = mapped_column(Text, server_default="[]")
