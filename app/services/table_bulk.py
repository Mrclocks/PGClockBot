"""Bulk table operations for web panel — parallel to per-row actions."""

from __future__ import annotations

from app.services.customer_identity import set_legacy_customer_blocked

import logging
from typing import Iterable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentStatus, Ticket
from app.services.shop_scope import platform_may_review_payment
from app.services.ticket_scope import staff_ticket_in_scope

logger = logging.getLogger(__name__)

MAX_BULK = 50


def parse_bulk_ids(raw: Iterable[str | int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if n <= 0 or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= MAX_BULK:
            break
    return out


def sanitize_return_to(raw: str, *, default: str) -> str:
    """Allow only same-origin relative paths (no open redirects / scheme-relative)."""
    path = (raw or "").strip() or default
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "://" in path
        or "\\" in path
        or any(ord(ch) < 32 for ch in path)
    ):
        return default
    parts = urlsplit(path)
    # Drop flash/cache bust params so we can re-append cleanly
    q = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in {"ok", "err", "_"}
    ]
    return urlunsplit(("", "", parts.path, urlencode(q), ""))


def redirect_bulk(return_to: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    import time

    q: list[tuple[str, str]] = []
    if ok:
        q.append(("ok", ok))
    if err:
        q.append(("err", err))
    q.append(("_", str(int(time.time()))))
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(
        f"{return_to}{sep}{urlencode(q, quote_via=quote)}",
        status_code=303,
    )


def _payment_in_scope(staff: dict, payment: Payment, order: Order | None) -> bool:
    from app.services.shop_scope import ShopScopeError, require_shop_owner_id

    if staff.get("role") == "admin":
        return platform_may_review_payment(payment, order)
    try:
        rid = require_shop_owner_id(staff)
    except ShopScopeError:
        return False
    if payment.is_wallet_topup:
        return False
    if not order or order.reseller_id != rid:
        return False
    return True


def order_bulk_ops(order: Order, payment: Payment | None) -> set[str]:
    """Mirror finance.html row-action eligibility (excludes receipt view)."""
    ops: set[str] = set()
    pay_pending = bool(payment and payment.status == PaymentStatus.PENDING.value)
    st = order.status
    if pay_pending:
        return {"approve", "reject", "cancel"}
    if st == OrderStatus.PAID.value:
        ops.add("approve")
    elif st == OrderStatus.AWAITING_APPROVAL.value:
        ops.update({"approve", "reject", "cancel"})
    elif st in {OrderStatus.PENDING.value, OrderStatus.AWAITING_RECEIPT.value}:
        ops.update({"approve", "reject", "cancel"})
    elif st == OrderStatus.REJECTED.value:
        ops.add("cancel")
    return ops


def _notice_shop_id(staff: dict) -> int | None:
    """The shop whose staff act, so their notices stay in that shop; None for platform staff."""
    from app.services.shop_scope import is_platform_admin, shop_owner_id

    return None if is_platform_admin(staff) else shop_owner_id(staff)


async def bulk_toggle_block(
    session: AsyncSession,
    staff: dict,
    user_ids: list[int],
    *,
    block: bool,
) -> tuple[int, int]:
    from app.services.notifications import actor_label_from_staff, notify_account_edit
    from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope
    from app.services.users import is_protected_admin

    ids = parse_bulk_ids(user_ids)
    ok = fail = 0
    actor = actor_label_from_staff(staff)
    notice_shop = _notice_shop_id(staff)
    for uid in ids:
        user = await session.get(BotUser, uid)
        if not user:
            fail += 1
            continue
        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError:
            fail += 1
            continue
        if is_protected_admin(user):
            fail += 1
            continue
        if block and user.is_blocked:
            fail += 1
            continue
        if not block and not user.is_blocked:
            fail += 1
            continue
        await set_legacy_customer_blocked(session, user, blocked=block)
        await session.flush()
        try:
            await notify_account_edit(
                session,
                user=user,
                event="block" if block else "unblock",
                actor=actor,
                shop_id=notice_shop,
            )
        except Exception:
            logger.debug("bulk block notify failed uid=%s", uid, exc_info=True)
        ok += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_delete_users(
    session: AsyncSession,
    staff: dict,
    user_ids: list[int],
    *,
    reason: str,
) -> tuple[int, int]:
    from app.services.notifications import actor_label_from_staff, notify_account_edit
    from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope
    from app.services.users import delete_bot_user, is_protected_admin

    reason = (reason or "").strip()
    if len(reason) < 3:
        return 0, len(parse_bulk_ids(user_ids))

    ids = parse_bulk_ids(user_ids)
    ok = fail = 0
    actor = actor_label_from_staff(staff)
    notice_shop = _notice_shop_id(staff)
    actor_id = None
    try:
        actor_id = int(staff.get("user_id") or 0) or None
    except (TypeError, ValueError):
        actor_id = None

    for uid in ids:
        user = await session.get(BotUser, uid)
        if not user:
            fail += 1
            continue
        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError:
            fail += 1
            continue
        if is_protected_admin(user) or user.role == "admin":
            fail += 1
            continue
        try:
            await notify_account_edit(
                session,
                user=user,
                event="user_delete",
                reason=reason,
                actor=actor,
                shop_id=notice_shop,
            )
            await delete_bot_user(session, uid, actor_user_id=actor_id)
            ok += 1
        except Exception:
            logger.debug("bulk delete failed uid=%s", uid, exc_info=True)
            fail += 1
            try:
                await session.rollback()
            except Exception:
                pass
    if ok:
        try:
            await session.commit()
        except Exception:
            logger.exception("bulk delete commit failed")
    return ok, fail


async def bulk_renew_users(
    session: AsyncSession,
    staff: dict,
    user_ids: list[int],
) -> tuple[int, int]:
    from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope, resolve_shop_scope_id
    from app.services.users_quick import quick_renew_user

    ids = parse_bulk_ids(user_ids)
    ok = fail = 0
    for uid in ids:
        user = await session.get(BotUser, uid)
        if not user:
            fail += 1
            continue
        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError:
            fail += 1
            continue
        try:
            await quick_renew_user(
                session, user, service_id=None, staff=staff, shop_id=resolve_shop_scope_id(staff)
            )
            ok += 1
        except Exception:
            logger.debug("bulk renew failed uid=%s", uid, exc_info=True)
            fail += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_close_bot_tickets(
    session: AsyncSession,
    staff: dict,
    ticket_ids: list[int],
) -> tuple[int, int]:
    ids = parse_bulk_ids(ticket_ids)
    ok = fail = 0
    for tid in ids:
        ticket = await session.get(Ticket, tid)
        if not ticket or ticket.status == "closed":
            fail += 1
            continue
        if not staff_ticket_in_scope(staff, ticket):
            fail += 1
            continue
        ticket.status = "closed"
        ok += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_order_action(
    session: AsyncSession,
    staff: dict,
    order_ids: list[int],
    action: str,
) -> tuple[int, int]:
    from app.services.orders import (
        approve_payment,
        cancel_order,
        manual_fulfill_unpaid_order,
        reject_order,
    )
    from app.services.shop_scope import ShopScopeError, assert_order_in_scope

    ids = parse_bulk_ids(order_ids)
    ok = fail = 0
    for oid in ids:
        order = await session.get(Order, oid)
        if not order:
            fail += 1
            continue
        if order.reseller_id and staff.get("role") == "admin":
            fail += 1
            continue
        if staff.get("role") != "admin":
            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError:
                fail += 1
                continue
        result = await session.execute(
            select(Payment)
            .where(Payment.order_id == oid)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        payment = result.scalar_one_or_none()
        allowed = order_bulk_ops(order, payment)
        if action not in allowed:
            fail += 1
            continue
        try:
            if action == "approve":
                if payment and payment.status == PaymentStatus.PENDING.value:
                    # Mirror single-order approve: pending receipt needs payments perm.
                    if staff.get("role") != "admin":
                        perms = staff.get("permissions") or []
                        if "payments" not in perms:
                            fail += 1
                            continue
                    await approve_payment(session, payment, reviewer_tg=0)
                elif order.status == OrderStatus.PAID.value:
                    # Must route renew/addon/reseller_app — deliver_order alone
                    # would seal mutation orders (service_id set) without applying.
                    from app.services.orders import fulfill_paid_order

                    await fulfill_paid_order(session, order)
                elif order.status in {
                    OrderStatus.PENDING.value,
                    OrderStatus.AWAITING_RECEIPT.value,
                }:
                    await manual_fulfill_unpaid_order(session, order, note="web bulk approve")
                elif order.status == OrderStatus.AWAITING_APPROVAL.value:
                    from app.services.orders import fulfill_paid_order

                    # Pending staff approval with no receipt path — mark PAID then fulfill.
                    if order.status != OrderStatus.PAID.value:
                        order.status = OrderStatus.PAID.value
                        await session.flush()
                    await fulfill_paid_order(session, order)
                else:
                    fail += 1
                    continue
            elif action == "reject":
                await reject_order(session, order, note="web bulk reject")
            elif action == "cancel":
                await cancel_order(session, order)
            else:
                fail += 1
                continue
            ok += 1
        except Exception:
            logger.debug("bulk order action failed oid=%s", oid, exc_info=True)
            fail += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_payment_action(
    session: AsyncSession,
    staff: dict,
    payment_ids: list[int],
    action: str,
) -> tuple[int, int]:
    from app.services.orders import approve_payment, reject_payment

    ids = parse_bulk_ids(payment_ids)
    ok = fail = 0
    for pid in ids:
        payment = await session.get(Payment, pid)
        if not payment or payment.status != PaymentStatus.PENDING.value:
            fail += 1
            continue
        order = await session.get(Order, payment.order_id) if payment.order_id else None
        if not _payment_in_scope(staff, payment, order):
            fail += 1
            continue
        try:
            if action == "approve":
                await approve_payment(session, payment, reviewer_tg=0)
            elif action == "reject":
                await reject_payment(session, payment, reviewer_tg=0)
            else:
                fail += 1
                continue
            ok += 1
        except Exception:
            logger.debug("bulk payment action failed pid=%s", pid, exc_info=True)
            fail += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_retry_delivery(
    session: AsyncSession,
    staff: dict,
    order_ids: list[int],
) -> tuple[int, int]:
    """Retry failed deliveries — mirrors /orders/{id}/retry-delivery."""
    from app.services.shop_scope import ShopScopeError, assert_order_retry_in_scope
    from app.services.ux20 import retry_delivery

    ids = parse_bulk_ids(order_ids)
    ok = fail = 0
    for oid in ids:
        order = await session.get(Order, oid)
        if not order:
            fail += 1
            continue
        try:
            assert_order_retry_in_scope(staff, order)
        except ShopScopeError:
            fail += 1
            continue
        try:
            await retry_delivery(session, oid)
            ok += 1
        except Exception:
            logger.debug("bulk retry delivery failed oid=%s", oid, exc_info=True)
            fail += 1
    return ok, fail
