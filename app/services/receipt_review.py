"""Receipt review outside the bot chat (Mini App) for platform admins and shop owners.

A platform admin reviews platform payments; a shop owner with the shop's ``payments``
permission reviews that shop's payments. Every action re-checks the payment with the
bot's ``reseller_can_review_payment`` under the reviewer's shop, then uses the bot's
claim-based approve and reject, so a payment is approved or rejected once whoever
taps first, in the bot or here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment, Plan, Role
from app.services.miniapp_auth import resolve_mini_persona
from app.services.orders import reject_payment
from app.services.payer_notify import notify_payer
from app.services.payment_review_actions import execute_payment_approve
from app.services.receipt_files import fetch_telegram_file, payment_bot_token
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user
from app.services.resellers import get_reseller_profile, has_bot_perm, reseller_can_review_payment
from app.services.shop_scope import payment_shop_id, pending_receipt_clause
from app.services.users import get_all_settings, reset_shop_reseller_id, set_shop_reseller_id

logger = logging.getLogger(__name__)

QUEUE_LIMIT = 20
DEFAULT_REJECT_TEXT = "پرداخت شما رد شد. اگر اشتباهی رخ داده با پشتیبانی در تماس باشید."
STARS_RECEIPT_PREFIX = "stars:"


class ReviewError(ValueError):
    """A review rule the reviewer must see (Persian, safe to show)."""


class ReviewDenied(ReviewError):
    """The caller does not review receipts here."""


class ReviewNotFound(ReviewError):
    """No such payment in the reviewer's scope (existence is not revealed)."""


@dataclass(slots=True)
class Reviewer:
    user: BotUser
    shop_id: int | None


async def mini_reviewer(session: AsyncSession, user: BotUser) -> Reviewer:
    """The caller's review scope: the platform for an admin, their own shop for a shop owner."""
    persona = resolve_mini_persona(user)
    if persona == "admin" and (user.role or "") == Role.ADMIN.value:
        return Reviewer(user=user, shop_id=None)
    if persona == "reseller":
        profile = await get_reseller_profile(session, int(user.id))
        if profile is not None and has_bot_perm(profile, "payments"):
            return Reviewer(user=user, shop_id=int(user.id))
    raise ReviewDenied("بررسی رسیدها برای حساب شما فعال نیست")


async def may_review_receipts(session: AsyncSession, user: BotUser) -> bool:
    try:
        await mini_reviewer(session, user)
    except ReviewDenied:
        return False
    return True


def _iso(value) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def _payer_label(user: BotUser | None) -> str:
    if user is None:
        return ""
    if user.full_name:
        return user.full_name
    return f"@{user.username}" if user.username else str(user.telegram_id or "")


def _queue_scope(shop_id: int | None):
    return (
        select(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(pending_receipt_clause(shop_id), ~Payment.receipt_file_id.startswith(STARS_RECEIPT_PREFIX))
    )


async def review_queue(session: AsyncSession, reviewer: Reviewer, *, limit: int = QUEUE_LIMIT) -> dict:
    """Oldest receipts first, with the payer and plan loaded in the same query."""
    scope = _queue_scope(reviewer.shop_id)
    total = await session.scalar(select(func.count()).select_from(scope.subquery()))
    rows = (
        await session.execute(
            scope.add_columns(BotUser, Plan.name)
            .outerjoin(BotUser, BotUser.id == Payment.user_id)
            .outerjoin(Plan, Plan.id == Order.plan_id)
            .order_by(Payment.id.asc())
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": int(payment.id),
            "amount": int(payment.amount or 0),
            "kind": "topup" if payment.is_wallet_topup else "order",
            "order_id": int(payment.order_id) if payment.order_id else None,
            "plan_name": plan_name or "",
            "payer": _payer_label(payer),
            "created_at": _iso(payment.created_at),
        }
        for payment, payer, plan_name in rows
    ]
    return {"items": items, "total": int(total or 0)}


async def _reviewable_payment(session: AsyncSession, reviewer: Reviewer, payment_id: int) -> tuple[Payment, Order | None]:
    payment = await session.get(Payment, int(payment_id))
    if payment is None:
        raise ReviewNotFound("پرداخت پیدا نشد")
    token = set_shop_reseller_id(reviewer.shop_id)
    try:
        allowed = await reseller_can_review_payment(session, reviewer.user, payment)
    finally:
        reset_shop_reseller_id(token)
    if not allowed:
        raise ReviewNotFound("پرداخت پیدا نشد")
    order = await session.get(Order, int(payment.order_id)) if payment.order_id else None
    return payment, order


async def receipt_image(session: AsyncSession, reviewer: Reviewer, payment_id: int) -> tuple[bytes, str]:
    """The receipt photo, read with the token of the bot that received it."""
    payment, order = await _reviewable_payment(session, reviewer, payment_id)
    file_id = (payment.receipt_file_id or "").strip()
    if not file_id or file_id.startswith(STARS_RECEIPT_PREFIX):
        raise ReviewNotFound("این پرداخت رسید تصویری ندارد")
    token = await payment_bot_token(session, payment, order=order, payer_reseller_id=payment_shop_id(payment, order))
    if not token:
        raise ReviewError("ربات این پرداخت در دسترس نیست")
    try:
        fetched = await fetch_telegram_file(token, file_id)
    except (httpx.HTTPError, ValueError):
        logger.warning("mini review receipt download failed payment=%s", payment_id, exc_info=True)
        fetched = None
    if fetched is None:
        raise ReviewError("دریافت رسید از تلگرام ناموفق بود")
    return fetched


async def _open_payer_bot(session: AsyncSession, payment: Payment, order: Order | None):
    payer = await session.get(BotUser, int(payment.user_id))
    try:
        return await open_notify_bot_for_user(session, payer, shop_id=payment_shop_id(payment, order))
    except ShopBotUnavailable as exc:
        raise ReviewError("ربات فروشگاه برای اطلاع‌رسانی به مشتری در دسترس نیست") from exc


async def approve_receipt(session: AsyncSession, reviewer: Reviewer, payment_id: int) -> str:
    """Approve once (credit or deliver, then tell the payer); returns the reviewer's message."""
    payment, order = await _reviewable_payment(session, reviewer, payment_id)
    bot, should_close = await _open_payer_bot(session, payment, order)
    try:
        result = await execute_payment_approve(
            session, payment, reviewer_tg=int(reviewer.user.telegram_id), bot=bot, source="mini:approve",
        )
    finally:
        if should_close:
            await bot.session.close()
    if not result.ok:
        raise ReviewError(result.alert_fa or "تأیید ناموفق بود")
    return result.alert_fa


async def reject_receipt(session: AsyncSession, reviewer: Reviewer, payment_id: int) -> None:
    """Reject once and tell the payer with the shop's reject text."""
    payment, order = await _reviewable_payment(session, reviewer, payment_id)
    shop_id = payment_shop_id(payment, order)
    payer_id = int(payment.user_id)
    bot, should_close = await _open_payer_bot(session, payment, order)
    try:
        await reject_payment(session, payment, int(reviewer.user.telegram_id), "rejected", bot=bot)
    except ValueError as exc:
        raise ReviewError(str(exc) or "این پرداخت قابل رد نیست") from exc
    finally:
        if should_close:
            await bot.session.close()
    body = (await get_all_settings(session, reseller_id=shop_id)).get("payment_reject_text") or DEFAULT_REJECT_TEXT
    await notify_payer(session, payer_id, title="❌ پرداخت رد شد", body=body, shop_id=shop_id)
