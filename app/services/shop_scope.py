"""Fail-closed shop tenant scoping for web-panel staff.

Resellers (including PG admins provisioned with a reseller plan) must only
see/mutate rows owned by their ``bot_user_id``. Missing scope must NEVER fall
through to the platform/admin catalog or global counts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, and_, or_

from app.db.models import Order, Payment, PaymentStatus


class ShopScopeError(Exception):
    """Raised when a non-admin staff session has no valid shop owner id."""

    def __init__(self, message: str = "محدوده فروشگاه برای این حساب مشخص نیست"):
        self.message = message
        super().__init__(message)


def shop_owner_id(staff: dict | None) -> int | None:
    """Return positive reseller bot_user_id, or None when there is no shop tenant id.

    - Explicit Owner Principal → None (platform shop catalog)
    - Bare ``role=admin`` without Owner → None meaning *no shop access*
      (never treat as platform; pair with ``is_platform_admin``)
    - ``reseller`` with valid bot_user_id → that id
    - anything else (pg_staff, broken session) → None meaning *no shop access*
      (callers must treat non-Owner + None as empty/deny, never as platform)
    """
    if not staff:
        return None
    from app.services.platform_identity import is_explicit_owner_staff

    if is_explicit_owner_staff(staff):
        return None
    role = staff.get("role")
    if role == "admin":
        # Phase 1G: sticky/legacy role alone is not platform catalog scope.
        return None
    if role == "reseller":
        try:
            rid = int(staff.get("bot_user_id") or 0)
        except (TypeError, ValueError):
            return None
        return rid if rid > 0 else None
    if role == "principal":
        try:
            rpid = int(staff.get("reseller_profile_id") or 0)
        except (TypeError, ValueError):
            rpid = 0
        if rpid <= 0:
            return None
        try:
            rid = int(staff.get("bot_user_id") or 0)
        except (TypeError, ValueError):
            return None
        return rid if rid > 0 else None
    return None


def is_platform_admin(staff: dict | None) -> bool:
    """Platform shop/tenant privileges — explicit Owner Principal only (Phase 1G).

    Bare ``role=admin`` is a legacy session label (see ``is_web_platform_admin``)
    and must not grant cross-tenant or platform-catalog authority by itself.
    """
    from app.services.platform_identity import is_explicit_owner_staff

    return is_explicit_owner_staff(staff)


def require_shop_owner_id(staff: dict) -> int:
    """Reseller must have a shop id; admin must not call this for shop mutations."""
    if is_platform_admin(staff):
        raise ShopScopeError("ادمین اصلی از این مسیر فروشگاهی استفاده نمی‌کند")
    rid = shop_owner_id(staff)
    if not rid:
        raise ShopScopeError(
            "حساب شما به فروشگاه متصل نیست — دسترسی به داده‌های ادمین اصلی مجاز نیست"
        )
    return rid


def _reseller_id_of(resource: Any) -> int | None:
    """Extract shop tenant id from a row. None = platform shop. Raises if unknown."""
    if resource is None:
        raise ShopScopeError("منبع یافت نشد")
    if not hasattr(resource, "reseller_id"):
        raise ShopScopeError("مالکیت منبع نامشخص است")
    raw = getattr(resource, "reseller_id", None)
    if raw is None:
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise ShopScopeError("مالکیت منبع نامشخص است") from exc
    if val <= 0:
        raise ShopScopeError("مالکیت منبع نامشخص است")
    return val


def assert_order_in_scope(staff: dict, order: Any) -> None:
    """Reseller/scoped staff may only touch orders in their shop.

    Platform admin bypasses here (visibility helper). Mutation paths that must
    forbid Owner acting on tenant orders use ``assert_order_retry_in_scope``
    (or an explicit local-safety check) instead.
    """
    if is_platform_admin(staff):
        return
    rid = require_shop_owner_id(staff)
    own = _reseller_id_of(order)
    own_i = int(own or 0)
    if own_i != rid:
        raise ShopScopeError("دسترسی به این سفارش ندارید")


def assert_order_retry_in_scope(staff: dict, order: Any) -> None:
    """H2 — retry-delivery object scope + Owner local safety.

    Visibility ≠ mutation: Owner may *see* shop context elsewhere, but must not
    retry tenant (``reseller_id`` set) orders — same product policy as
    ``order_approve``. Sibling shops never cross by ``order_id``.
    """
    own = _reseller_id_of(order)
    if is_platform_admin(staff):
        if own is not None:
            raise ShopScopeError(
                "این سفارش مربوط به نماینده است — فقط در پنل همان فروشگاه قابل تحویل مجدد است"
            )
        return
    rid = require_shop_owner_id(staff)
    if own is None or int(own) != int(rid):
        raise ShopScopeError("دسترسی به این سفارش ندارید")


def payment_shop_id(payment: Any, order: Any | None) -> int | None:
    """The shop a payment belongs to: a top-up's purse, else its order's shop (None = platform)."""
    if getattr(payment, "is_wallet_topup", False):
        return getattr(payment, "wallet_shop_id", None)
    return _reseller_id_of(order) if order is not None else None


def pending_receipt_clause(shop_id: int | None) -> ColumnElement[bool]:
    """Payments with a receipt waiting for review in one shop (None = platform).

    The caller outer-joins ``Order`` on ``Payment.order_id``; a top-up belongs to its
    wallet's shop and an order payment to its order's shop, as in ``payment_shop_id``.
    """
    topup_shop = Payment.wallet_shop_id.is_(None) if shop_id is None else Payment.wallet_shop_id == int(shop_id)
    order_shop = Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)
    return and_(
        Payment.status == PaymentStatus.PENDING.value,
        Payment.receipt_file_id.is_not(None),
        or_(
            and_(Payment.is_wallet_topup.is_(True), topup_shop),
            and_(Payment.is_wallet_topup.is_(False), order_shop),
        ),
    )


def platform_may_review_payment(payment: Any, order: Any | None) -> bool:
    """Owner reviews platform payments only; shop wallets and shop orders stay with that shop.

    Mirrors ``reseller_can_review_payment`` on the bot so the web panel cannot
    credit or reject another shop's money.
    """
    return payment_shop_id(payment, order) is None


def assert_bot_user_in_scope(staff: dict, user: Any) -> None:
    """H3 — BotUser object scope (users / wallet / loyalty targets).

    Ownership = ``BotUser.reseller_id`` (platform shop when NULL). Never uses
    PasarGuard role names. Missing/unknown ownership → deny.
    Platform admin → platform-shop users only (local safety; no tenant cross).
    """
    own = _reseller_id_of(user)
    if is_platform_admin(staff):
        if own is not None:
            raise ShopScopeError(
                "این کاربر متعلق به فروشگاه نماینده است — در پنل همان فروشگاه مدیریت شود"
            )
        return
    rid = require_shop_owner_id(staff)
    if own is None or int(own) != int(rid):
        raise ShopScopeError("دسترسی به این کاربر ندارید")


def bot_user_in_scope(staff: dict, user: Any) -> bool:
    try:
        assert_bot_user_in_scope(staff, user)
        return True
    except ShopScopeError:
        return False


def resolve_shop_scope_id(staff: dict) -> int | None:
    """Platform admin → None (platform shop). Reseller → bot_user_id.

    Scopeless non-admin (pg_staff, broken reseller) → ShopScopeError (never
    fall through to platform).
    """
    if is_platform_admin(staff):
        return None
    return require_shop_owner_id(staff)


def empty_shop_stats() -> dict[str, int]:
    return {
        "users": 0,
        "orders": 0,
        "pending": 0,
        "services": 0,
        "revenue": 0,
        "plans": 0,
        "tickets": 0,
    }
