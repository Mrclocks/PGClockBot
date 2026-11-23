"""Service cancellations under the shop's cancellation settings, serialized with paid service mutations.

A request records the refund the shop's rule gives at that moment. With manual approval that amount
is a suggestion the operator may change; with automatic approval the request is approved at once
with it, unless the amount is over the shop's automatic cap or the panel could not be read, in which
case it waits for an operator like a manual request.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, ServiceAutomation, ServiceCancellation, ShopWallet, UserService
from app.services.cashback import reverse_after_refund
from app.services.cancellation_customer_notice import notify_cancellation_decision
from app.services.cancellation_refund import RefundQuote, outside_window, period_payment, quote_refund, remaining_share
from app.services.pasarguard import PasarGuardError, get_pg, get_pg_for_reseller
from app.services.service_cancellation_settings import AUTO_ACTOR, CancellationPolicy, cancellation_policy
from app.services.service_automation import service_shop_id
from app.services.service_freeze_guard import assert_not_frozen
from app.services.service_transfer_guard import assert_not_transferred
from app.services.service_import import is_external_service
from app.services.wallet import credit_wallet

STATUS_LABELS = {"pending": "در انتظار بررسی", "processing": "در حال غیرفعال‌سازی", "review": "نیازمند بررسی و تلاش مجدد", "approved": "لغو شد؛ اعتبار به کیف پول همین فروشگاه برگشت", "rejected": "درخواست رد شد", "withdrawn": "درخواست پس گرفته شد"}
# Operator-facing queue — badge + list filter for items needing human action.
OPEN_OPERATOR_STATUSES = ("pending", "review")
# A request in one of these blocks a new one for the same service.
OPEN_REQUEST_STATUSES = frozenset({"pending", "processing", "review", "approved"})
DISABLED_MESSAGE = "لغو سرویس برای این سرویس فعال نیست"
WINDOW_MESSAGE = "مهلت لغو این سرویس تمام شده است"

logger = logging.getLogger(__name__)


async def lock_service_mutation(session, service_id):
    """Lock the service row in the caller's transaction before claiming an order."""
    result = await session.execute(update(UserService).where(
        UserService.id == service_id, UserService.is_cancelled.is_(False),
        UserService.cancellation_pending.is_(False),
    ).values(is_cancelled=False).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ValueError("این سرویس لغو شده یا لغو آن در حال بررسی است")


async def owned_cancellation_service(session, user, service_id, shop_id):
    service = await session.get(UserService, service_id)
    if not service or service.bot_user_id != user.id or user.is_blocked:
        raise ValueError("سرویس یافت نشد")
    if not service.pg_user_id or is_external_service(service):
        raise ValueError("لغو این سرویس از این مسیر ممکن نیست")
    if await service_shop_id(session, service) != shop_id:
        raise ValueError("درخواست را از فروشگاه خود سرویس ثبت کنید")
    return service


async def cancellation_status(session, user, service_id, *, shop_id):
    await owned_cancellation_service(session, user, service_id, shop_id)
    row = await session.scalar(select(ServiceCancellation).where(
        ServiceCancellation.service_id == service_id,
        ServiceCancellation.user_id == user.id,
        ServiceCancellation.reseller_id == shop_id,
    ).order_by(ServiceCancellation.id.desc()).limit(1))
    return {"id": row.id, "status": row.status, "label": STATUS_LABELS[row.status],
            "reason": row.reason, "refund_amount": row.refund_amount if row.status == "approved" else None,
            "operator_note": row.operator_note} if row else None


@dataclass(frozen=True, slots=True)
class CancellationOffer:
    """What the customer's cancellation screen shows for one service."""

    status: dict[str, Any] | None
    policy: CancellationPolicy
    # Why a new request cannot be made; None when it can.
    blocked_reason: str | None
    # The refund the shop's rule gives now; None when a request cannot be made or the panel did not answer.
    quote: RefundQuote | None

    @property
    def can_request(self) -> bool:
        return self.blocked_reason is None


def shows_cancel_button(service: UserService, ui: Mapping[str, Any]) -> bool:
    """The service's «⚙️ بیشتر» shows the cancel entry while the shop allows it or a request is open."""
    if not service.pg_user_id or is_external_service(service):
        return False
    return bool(service.cancellation_pending or service.is_cancelled) or cancellation_policy(ui).allows_plan(service.plan_id)


async def _panel_info(session: AsyncSession, service: UserService, shop_id: int | None) -> dict[str, Any] | None:
    """The panel user of ``service``, or None when the panel did not answer for that exact user."""
    try:
        pg = await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()
        info = await pg.get_user_by_id(int(service.pg_user_id))
    except (PasarGuardError, httpx.HTTPError, TimeoutError, ValueError):
        logger.warning("cancellation quote panel read failed service=%s", service.id, exc_info=True)
        return None
    if not isinstance(info, dict) or int(info.get("id") or 0) != int(service.pg_user_id):
        return None
    return info


async def _quote(
    session: AsyncSession, service: UserService, policy: CancellationPolicy, *, shop_id: int | None, now: datetime,
) -> tuple[str | None, RefundQuote | None]:
    """(blocked reason, quote) of a new request under ``policy``; the panel is read only when needed."""
    payment = await period_payment(session, service)
    if outside_window(policy, payment, service, now):
        return WINDOW_MESSAGE, None
    info = await _panel_info(session, service, shop_id)
    if info is None:
        return None, None
    return None, quote_refund(policy, payment, remaining_share(info, service, payment.started_at, now))


async def _request_block(session: AsyncSession, service: UserService, policy: CancellationPolicy) -> str | None:
    if not policy.allows_plan(service.plan_id):
        return DISABLED_MESSAGE
    try:
        await assert_not_frozen(session, service.id)
        await assert_not_transferred(session, service.id)
    except ValueError as exc:
        return str(exc)
    return None


async def cancellation_offer(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, ui: Mapping[str, Any],
) -> CancellationOffer:
    """The customer's current request, if any, and whether and for how much a new one can be made."""
    service = await owned_cancellation_service(session, user, service_id, shop_id)
    policy = cancellation_policy(ui)
    status = await cancellation_status(session, user, service_id, shop_id=shop_id)
    if status and status["status"] in OPEN_REQUEST_STATUSES:
        return CancellationOffer(status, policy, "برای این سرویس درخواست لغو ثبت شده است", None)
    blocked = await _request_block(session, service, policy)
    if blocked:
        return CancellationOffer(status, policy, blocked, None)
    blocked, quote = await _quote(session, service, policy, shop_id=shop_id, now=datetime.now(timezone.utc))
    return CancellationOffer(status, policy, blocked, quote)


async def _insert_request(
    session: AsyncSession, user: BotUser, service: UserService, *, shop_id: int | None, reason: str,
    quote: RefundQuote | None,
) -> None:
    user_id = int(user.id)
    session.add(ServiceCancellation(
        service_id=service.id, user_id=user_id, reseller_id=shop_id, reason=reason, pg_user_id=service.pg_user_id,
        paid_amount=quote.paid_amount if quote else None,
        remaining_percent=quote.remaining_percent if quote else None,
        suggested_refund=quote.amount if quote else None,
    ))
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent request for the same service won; the caller reads that one.
        await session.rollback()


async def _approve_automatically(session: AsyncSession, request_id: int, *, shop_id: int | None, amount: int) -> None:
    """Approve with the quoted amount; a failure leaves the request for an operator as it stands."""
    try:
        await approve_cancellation(session, request_id, shop_id=shop_id, amount=amount, actor=AUTO_ACTOR)
    except ValueError:
        await session.rollback()
        logger.warning("automatic cancellation approval left for review request=%s", request_id, exc_info=True)


async def request_cancellation(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, reason: object,
    ui: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the customer's request under the shop's settings and approve it at once when they say so."""
    user_id = int(user.id)
    reason = str(reason or "").strip()
    if not reason or len(reason) > 1000:
        raise ValueError("دلیل لغو را بین ۱ تا ۱۰۰۰ نویسه بنویسید")
    service = await owned_cancellation_service(session, user, service_id, shop_id)
    existing = await cancellation_status(session, user, service_id, shop_id=shop_id)
    if existing and existing["status"] in OPEN_REQUEST_STATUSES:
        return existing
    policy = cancellation_policy(ui)
    blocked = await _request_block(session, service, policy)
    if blocked:
        raise ValueError(blocked)
    blocked, quote = await _quote(session, service, policy, shop_id=shop_id, now=datetime.now(timezone.utc))
    if blocked:
        raise ValueError(blocked)
    await _insert_request(session, user, service, shop_id=shop_id, reason=reason, quote=quote)
    user = await session.get(BotUser, user_id)
    status = await cancellation_status(session, user, service_id, shop_id=shop_id)
    if status is None:
        raise ValueError("درخواست قبلی این سرویس نیاز به بررسی پشتیبانی دارد")
    if status["status"] == "pending" and quote is not None and policy.auto_allows(quote.amount):
        await _approve_automatically(session, int(status["id"]), shop_id=shop_id, amount=quote.amount)
        user = await session.get(BotUser, user_id)
        status = await cancellation_status(session, user, service_id, shop_id=shop_id)
    return status


def assert_cancellation_scope(row, shop_id):
    if not row or row.reseller_id != shop_id:
        raise ValueError("درخواست یافت نشد")


async def pending_cancellation_count(session, shop_id) -> int:
    """Count open cancellation requests for one shop (platform shop_id is None)."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ServiceCancellation)
            .where(
                ServiceCancellation.reseller_id == shop_id,
                ServiceCancellation.status.in_(OPEN_OPERATOR_STATUSES),
            )
        )
        or 0
    )


async def has_pending_cancellations(session, shop_id) -> bool:
    return (await pending_cancellation_count(session, shop_id)) > 0


async def list_cancellation_requests(session, shop_id, *, before: int | None = None, limit: int = 50):
    """Shop-scoped cancellation rows newest-first. Returns (rows, next_before)."""
    limit = max(1, min(int(limit), 100))
    query = (
        select(ServiceCancellation, UserService.pg_username)
        .join(UserService, UserService.id == ServiceCancellation.service_id)
        .where(ServiceCancellation.reseller_id == shop_id)
    )
    if before is not None:
        query = query.where(ServiceCancellation.id < int(before))
    rows = list(
        (
            await session.execute(
                query.order_by(ServiceCancellation.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    next_before = int(rows[limit - 1][0].id) if len(rows) > limit else None
    return rows[:limit], next_before


async def reject_cancellation(session, request_id, *, shop_id, actor, note):
    row = await session.get(ServiceCancellation, request_id)
    assert_cancellation_scope(row, shop_id)
    if len(note) > 1000:
        raise ValueError("توضیح اپراتور طولانی است")
    result = await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.id == request_id, ServiceCancellation.status == "pending",
    ).values(status="rejected", operator_note=note.strip(), processed_by=actor,
             processed_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        raise ValueError("فقط درخواست بررسی‌نشده قابل رد است")
    await session.commit()
    await notify_cancellation_decision(session, request_id)


async def approve_cancellation(session, request_id, *, shop_id, amount, actor, note=""):
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 2**31 - 1 or len(note) > 1000:
        raise ValueError("مبلغ یا توضیح اعتبار برگشتی نامعتبر است")
    row = await session.get(ServiceCancellation, request_id)
    assert_cancellation_scope(row, shop_id)
    if row.status == "approved":
        return row
    if row.refund_amount is not None and row.refund_amount != amount:
        raise ValueError("در تلاش مجدد، مبلغ تأییدشده قابل تغییر نیست")
    balance_query = select(ShopWallet.balance).where(ShopWallet.user_id == row.user_id, ShopWallet.reseller_id == shop_id) if shop_id else select(BotUser.wallet_balance).where(BotUser.id == row.user_id)
    if int(await session.scalar(balance_query) or 0) + amount > 2**31 - 1:
        raise ValueError("مجموع اعتبار برگشتی و موجودی از سقف کیف پول بیشتر است")
    service = await session.get(UserService, row.service_id)
    if (not service or service.bot_user_id != row.user_id or service.pg_user_id != row.pg_user_id
            or await service_shop_id(session, service) != shop_id):
        raise ValueError("مالکیت یا شناسهٔ پنل سرویس تغییر کرده است")
    now, token = datetime.now(timezone.utc), secrets.token_hex(16)
    claim = await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.id == request_id,
        ServiceCancellation.status.in_(["pending", "processing", "review"]),
        or_(ServiceCancellation.refund_amount.is_(None), ServiceCancellation.refund_amount == amount),
        or_(ServiceCancellation.locked_until.is_(None), ServiceCancellation.locked_until < now),
    ).values(status="processing", refund_amount=amount, operator_note=note.strip(),
             processed_by=actor, lock_token=token, locked_until=now + timedelta(minutes=5))
         .execution_options(synchronize_session=False))
    if claim.rowcount != 1:
        await session.rollback()
        raise ValueError("این درخواست در حال پردازش است یا بسته شده است")
    # Both cancellation and quota delivery take this row lock before checking orders.
    await session.execute(update(UserService).where(UserService.id == service.id).values(cancellation_pending=True))
    busy = await session.scalar(select(Order.id).where(
        Order.service_id == service.id,
        or_(Order.service_mutation_pending.is_(True), Order.status.not_in(["delivered", "rejected", "cancelled"])),
    ).limit(1))
    if busy:
        await session.rollback()
        raise ValueError("سفارش باز سرویس باید پیش از لغو تعیین تکلیف شود")
    await session.commit()
    try:
        pg = await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()
        info = await pg.get_user_by_id(row.pg_user_id)
        if int(info.get("id") or 0) != row.pg_user_id:
            raise ValueError("پاسخ پنل با سرویس همخوان نیست")
        if str(info.get("status") or "").lower() != "disabled":
            await pg.set_disabled_by_id(row.pg_user_id, True)
            info = await pg.get_user_by_id(row.pg_user_id)
            if int(info.get("id") or 0) != row.pg_user_id or str(info.get("status") or "").lower() != "disabled":
                raise ValueError("غیرفعال‌شدن سرویس در پنل تأیید نشد")
        sealed = await session.execute(update(ServiceCancellation).where(
            ServiceCancellation.id == request_id, ServiceCancellation.lock_token == token,
            ServiceCancellation.status == "processing",
        ).values(status="approved", processed_at=datetime.now(timezone.utc), lock_token=None, locked_until=None)
             .execution_options(synchronize_session=False))
        if sealed.rowcount != 1:
            await session.rollback()
            raise ValueError("نتیجه نیاز به بررسی اپراتور دارد")
        user = await session.get(BotUser, row.user_id)
        if amount:
            await credit_wallet(session, user, amount, f"اعتبار لغو سرویس #{service.id} (درخواست #{request_id})", shop_id=shop_id, commit=False)
        await session.execute(update(UserService).where(UserService.id == service.id).values(is_cancelled=True, cancellation_pending=False))
        await session.execute(update(ServiceAutomation).where(ServiceAutomation.service_id == service.id).values(
            renew_enabled=False, duration_enabled=False, volume_enabled=False))
        await session.commit()
        await session.refresh(row)
    except Exception:
        await session.rollback()
        await session.execute(update(ServiceCancellation).where(
            ServiceCancellation.id == request_id, ServiceCancellation.lock_token == token,
        ).values(status="review", lock_token=None, locked_until=None).execution_options(synchronize_session=False))
        await session.commit()
        raise ValueError("نتیجهٔ پنل نامشخص است؛ اعتبار واریز نشده و درخواست برای تلاش مجدد محفوظ است") from None
    # Reached only when this call moved the request to approved: the failure branch above raises.
    if amount:
        await reverse_after_refund(session, service_id=int(service.id), reason=f"لغو سرویس #{int(service.id)}")
    await notify_cancellation_decision(session, request_id)
    return row
