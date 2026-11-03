"""Capacity purchases paid like a plan: an order any enabled payment method can settle, applied once.

The order note freezes the entitlement (``res_cap:pack:{pack_id}:{gb}:{users}``, ``res_cap:gb:{n}``,
``res_cap:users:{n}``) and ``order.amount`` freezes the price, so a catalog change between the order and
its payment changes neither. Orders belong to the platform (no shop): the money goes to the platform's
payment methods, never to a shop's. Applying claims the paid order atomically, records the extra, then
raises the admin's live panel limit; a refusal puts the order back to paid for staff, and a lost answer
leaves it flagged for review instead of refunding or repeating.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, ResellerPlan, ResellerProfile
from app.services.db_safe import rollback_quiet
from app.services.reseller_capacity import (
    CapacityOffer,
    PanelRaiseRefused,
    PanelRaiseUnknown,
    ensure_base_gb_before_extra,
    raise_admin_limit,
    revert_extras,
    unit_offer,
)
from app.services.reseller_packs import pack_offer
from app.services.reseller_shop_gates import assert_shop_allows_capacity, assert_subscription_open
from app.services.service_renewals import RenewalPendingReview
from app.services.volume_allocation_cascade import assert_child_growth_fits

logger = logging.getLogger(__name__)

NOTE_PREFIX = "res_cap:"
KIND_PACK, KIND_GB, KIND_USERS = "pack", "gb", "users"
_NOTE_RE = re.compile(r"^res_cap:(pack|gb|users):(\d{1,10})(?::(\d{1,6}):(\d{1,6}))?$")

INVALID_ORDER = "سفارش ظرفیت نامعتبر است"
NOT_APPLIED = "پرداخت ثبت شد ولی اعمال ظرفیت در پاسارگارد پذیرفته نشد؛ پشتیبانی بررسی می‌کند. دوباره خرید نکنید."
NOT_FOR_SHOP_BOT = "پرداخت با روش‌های دیگر فقط در ربات اصلی ممکن است؛ از کیف پول استفاده کنید"


class CapacityPendingReview(RenewalPendingReview):
    """The panel did not answer: the capacity may be applied, so the payment stays and staff check."""

    def __init__(self) -> None:
        ValueError.__init__(
            self, "نتیجه اعمال ظرفیت نیاز به بررسی پشتیبانی دارد؛ پرداخت شما ثبت شده است. دوباره خرید نکنید.",
        )


@dataclass(frozen=True, slots=True)
class CapacityNote:
    kind: str
    ref: int
    gb: int
    users: int


def is_capacity_order_note(note: str | None) -> bool:
    return (note or "").strip().startswith(NOTE_PREFIX)


def format_note(kind: str, ref: int, *, gb: int = 0, users: int = 0) -> str:
    base = f"{NOTE_PREFIX}{kind}:{int(ref)}"
    return f"{base}:{int(gb)}:{int(users)}" if kind == KIND_PACK else base


def parse_note(note: str | None) -> CapacityNote | None:
    """The frozen entitlement of a capacity order, or None for any other (or a malformed) note."""
    match = _NOTE_RE.match((note or "").strip())
    if match is None:
        return None
    kind, ref = match.group(1), int(match.group(2))
    if kind == KIND_PACK:
        if match.group(3) is None:
            return None
        return CapacityNote(kind, ref, int(match.group(3)), int(match.group(4)))
    if match.group(3) is not None:
        return None
    return CapacityNote(kind, ref, ref if kind == KIND_GB else 0, ref if kind == KIND_USERS else 0)


def describe(parsed: CapacityNote) -> str:
    parts = [f"{parsed.gb} گیگ حجم" if parsed.gb else "", f"{parsed.users} کاربر" if parsed.users else ""]
    return " و ".join(part for part in parts if part)


async def create_capacity_order(
    session: AsyncSession,
    *,
    user: BotUser,
    profile: ResellerProfile,
    plan: ResellerPlan | None,
    kind: str,
    ref: int,
) -> Order:
    """A pending platform order for a pack (``ref`` = pack id) or a quick-buy (``ref`` = GB or users)."""
    await assert_shop_allows_capacity(session, profile)
    if kind == KIND_PACK:
        offer = await pack_offer(session, user=user, profile=profile, plan=plan, pack_id=ref)
        note = format_note(KIND_PACK, ref, gb=offer.gb, users=offer.users)
    elif kind in {KIND_GB, KIND_USERS}:
        offer = unit_offer(profile, plan, kind, ref)
        note = format_note(kind, ref)
    else:
        raise ValueError(INVALID_ORDER)
    if parse_note(note) is None:
        raise ValueError(INVALID_ORDER)
    order = Order(user_id=int(user.id), plan_id=None, amount=offer.price, status=OrderStatus.PENDING.value, note=note, reseller_id=None)
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def _set_state(session: AsyncSession, order_id: int, *, expected: str, status: str, pending: bool) -> bool:
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == expected)
        .values(status=status, service_mutation_pending=pending)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount == 1)


async def _offer_for_paid_order(session: AsyncSession, order: Order, parsed: CapacityNote) -> CapacityOffer:
    """Re-check the owner and the subscription at the point of mutation; the price is the paid amount."""
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, int(order.user_id))
    if profile is None or not profile.is_active:
        raise ValueError(INVALID_ORDER)
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        raise ValueError(INVALID_ORDER)
    await assert_subscription_open(session, uname)
    await assert_child_growth_fits(session, pg_username=uname, add_gb=parsed.gb)
    return CapacityOffer(uname=uname, gb=parsed.gb, users=parsed.users, price=int(order.amount), reason=f"reseller_order:{int(order.id)}")


async def apply_capacity_order(session: AsyncSession, order: Order) -> Order:
    """Apply a paid capacity order once; see the module docstring for the failure handling."""
    from app.services.pg_admin_subscription import record_unit_extra

    parsed = parse_note(order.note)
    if parsed is None or order.reseller_id is not None:
        raise ValueError(INVALID_ORDER)
    order_id = int(order.id)
    if not await _set_state(session, order_id, expected=OrderStatus.PAID.value, status=OrderStatus.DELIVERING.value, pending=True):
        await session.refresh(order)
        if order.status == OrderStatus.DELIVERED.value:
            return order
        raise ValueError("سفارش ظرفیت قابل پردازش نیست")
    await session.commit()
    try:
        offer = await _offer_for_paid_order(session, order, parsed)
        purchase = offer.purchase()
        await ensure_base_gb_before_extra(session, purchase)
        await record_unit_extra(session, pg_username=offer.uname, gb=offer.gb, users=offer.users)
        await session.commit()
    except Exception:
        await rollback_quiet(session)
        await _set_state(session, order_id, expected=OrderStatus.DELIVERING.value, status=OrderStatus.PAID.value, pending=False)
        await session.commit()
        raise
    try:
        await raise_admin_limit(purchase, offer.raiser())
    except PanelRaiseRefused as exc:
        await revert_extras(session, purchase)
        await _set_state(session, order_id, expected=OrderStatus.DELIVERING.value, status=OrderStatus.PAID.value, pending=False)
        await session.commit()
        raise ValueError(NOT_APPLIED) from exc
    except PanelRaiseUnknown as exc:
        raise CapacityPendingReview from exc
    await _set_state(session, order_id, expected=OrderStatus.DELIVERING.value, status=OrderStatus.DELIVERED.value, pending=False)
    await session.commit()
    await session.refresh(order)
    return order
