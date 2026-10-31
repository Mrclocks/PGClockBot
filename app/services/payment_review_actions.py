"""Phase 1 payment-review actions — approve / resume / resend.

Each action enforces diagnosis kind before mutating. Shared by bot handlers
and (where useful) web callers so bot + panel stay aligned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment
from app.services.delivery import send_delivery_to_user
from app.services.orders import approve_payment
from app.services.payment_review_diag import (
    PaymentReviewAction,
    PaymentReviewDiagnosis,
    diagnose_payment_review,
    log_payment_review_diagnosis,
)
from app.services.redact import redact, user_safe_error

logger = logging.getLogger(__name__)

TELEGRAM_ALERT_LIMIT = 200
FAILURE_DETAIL_LIMIT = 300
DELIVERY_FAILED_ALERT = "پرداخت تأیید شد ولی ساخت سرویس ناموفق بود"
DELIVERY_FAILED_HINT = "با «ادامه تحویل» دوباره تلاش کنید."


@dataclass(frozen=True)
class PaymentActionResult:
    ok: bool
    action: PaymentReviewAction
    diag: PaymentReviewDiagnosis
    order: Order | None = None
    alert_fa: str = ""
    message_suffix: str = ""
    error: str | None = None


async def _load_diag(
    session: AsyncSession, payment: Payment
) -> tuple[PaymentReviewDiagnosis, Order | None]:
    order = (
        await session.get(Order, int(payment.order_id)) if payment.order_id else None
    )
    diag = await diagnose_payment_review(session, payment, order=order)
    return diag, order


def _failed_delivery_alert(detail: str) -> str:
    """Reviewer alert: what failed and the next step, within Telegram's alert limit."""
    tail = f"\n{DELIVERY_FAILED_HINT}"
    room = TELEGRAM_ALERT_LIMIT - len(DELIVERY_FAILED_ALERT) - len(tail) - 2
    if not detail:
        return f"{DELIVERY_FAILED_ALERT}.{tail}"
    short = detail if len(detail) <= room else detail[: room - 1] + "…"
    return f"{DELIVERY_FAILED_ALERT}: {short}{tail}"


async def _report_failed_delivery(
    session: AsyncSession,
    *,
    bot: Any,
    payment: Payment,
    order: Order | None,
    exc: Exception,
) -> str:
    """The payment is approved but the service was not created: keep the reason, alert staff
    and tell the buyer, as the receipt and gateway paths do. Delivery is resumed by a person.
    """
    # Local imports: both modules import handler code that imports this one.
    from app.services.notifications import notify_approved_delivery_stuck
    from app.services.ux20 import note_delivery_send_failure

    recorded = redact(str(exc), limit=FAILURE_DETAIL_LIMIT)
    logger.warning("approved payment left undelivered payment=%s: %s", payment.id, recorded)
    await note_delivery_send_failure(session, order=order, payment=payment, error=recorded)
    buyer = await session.get(BotUser, int(payment.user_id)) if payment.user_id else None
    buyer_tg = getattr(buyer, "telegram_id", None)
    try:
        await notify_approved_delivery_stuck(bot, session, payment, buyer_tg, error=recorded)
    except Exception:
        # The failed-delivery list still shows the order to staff.
        logger.warning("undelivered staff notice failed payment=%s", payment.id, exc_info=True)
    if buyer_tg is not None and order is not None:
        try:
            await send_delivery_to_user(bot, buyer_tg, session, payment, order)
        except Exception:
            logger.warning("undelivered buyer notice failed payment=%s", payment.id, exc_info=True)
    return _failed_delivery_alert(
        user_safe_error(exc, fallback="", limit=FAILURE_DETAIL_LIMIT)
    )


async def execute_payment_approve(
    session: AsyncSession,
    payment: Payment,
    *,
    reviewer_tg: int,
    bot: Any,
    source: str = "payrev:ok",
) -> PaymentActionResult:
    """First-time PENDING claim only. Refuses resume/resend kinds."""
    diag, _order = await _load_diag(session, payment)
    log_payment_review_diagnosis(diag, actor_tg=reviewer_tg, source=source)
    if not diag.allows_action("approve"):
        return PaymentActionResult(
            ok=False,
            action="approve",
            diag=diag,
            alert_fa=diag.wrong_tool_alert("approve"),
            error="wrong_tool",
        )
    try:
        order = await approve_payment(session, payment, reviewer_tg, bot=bot)
    except Exception as exc:
        fresh = await session.get(Payment, int(payment.id))
        post, post_order = await _load_diag(session, fresh) if fresh else (diag, None)
        log_payment_review_diagnosis(
            post, actor_tg=reviewer_tg, source=f"{source}:error"
        )
        if post.kind == "approved_fulfill" and fresh is not None:
            alert_fa = await _report_failed_delivery(
                session, bot=bot, payment=fresh, order=post_order, exc=exc
            )
        elif post.kind != "pending":
            alert_fa = post.alert_fa
        else:
            alert_fa = f"خطا: {user_safe_error(exc)}"
        return PaymentActionResult(
            ok=False,
            action="approve",
            diag=post,
            alert_fa=alert_fa,
            error=str(exc),
        )
    send_err = await _deliver_and_notify(
        session, bot=bot, payment=payment, order=order, reviewer_tg=reviewer_tg
    )
    if send_err:
        return PaymentActionResult(
            ok=True,
            action="approve",
            diag=diag,
            order=order,
            alert_fa="تأیید شد — ارسال پیام به کاربر ناموفق بود",
            message_suffix="\n\n✅ تأیید شد (ارسال پیام ناموفق)",
            error=send_err,
        )
    return PaymentActionResult(
        ok=True,
        action="approve",
        diag=diag,
        order=order,
        alert_fa="تأیید شد ✅",
        message_suffix="\n\n✅ تأیید دستی شد",
    )


async def execute_payment_resume(
    session: AsyncSession,
    payment: Payment,
    *,
    reviewer_tg: int,
    bot: Any,
    source: str = "payrev:go",
) -> PaymentActionResult:
    """Resume incomplete APPROVED credit/fulfill. Never mints twice."""
    diag, _order = await _load_diag(session, payment)
    log_payment_review_diagnosis(diag, actor_tg=reviewer_tg, source=source)
    if not diag.allows_action("resume"):
        return PaymentActionResult(
            ok=False,
            action="resume",
            diag=diag,
            alert_fa=diag.wrong_tool_alert("resume"),
            error="wrong_tool",
        )
    try:
        order = await approve_payment(session, payment, reviewer_tg, bot=bot)
    except Exception as exc:
        fresh = await session.get(Payment, int(payment.id))
        post, _ = await _load_diag(session, fresh) if fresh else (diag, None)
        log_payment_review_diagnosis(
            post, actor_tg=reviewer_tg, source=f"{source}:error"
        )
        return PaymentActionResult(
            ok=False,
            action="resume",
            diag=post,
            alert_fa=post.alert_fa
            if post.preferred_action != "resume"
            else f"خطا: {user_safe_error(exc)}",
            error=str(exc),
        )
    send_err = await _deliver_and_notify(
        session, bot=bot, payment=payment, order=order, reviewer_tg=reviewer_tg
    )
    if send_err:
        return PaymentActionResult(
            ok=True,
            action="resume",
            diag=diag,
            order=order,
            alert_fa="تحویل ادامه یافت — ارسال پیام به کاربر ناموفق بود",
            message_suffix="\n\n♻️ تحویل ادامه یافت (ارسال پیام ناموفق)",
            error=send_err,
        )
    return PaymentActionResult(
        ok=True,
        action="resume",
        diag=diag,
        order=order,
        alert_fa="تحویل ادامه یافت ✅",
        message_suffix="\n\n♻️ تحویل ادامه یافت",
    )


async def execute_payment_resend(
    session: AsyncSession,
    payment: Payment,
    *,
    reviewer_tg: int,
    bot: Any,
    source: str = "payrev:send",
) -> PaymentActionResult:
    """Telegram re-send only — no approve claim, no mint, no wallet credit."""
    diag, order = await _load_diag(session, payment)
    log_payment_review_diagnosis(diag, actor_tg=reviewer_tg, source=source)
    if not diag.allows_action("resend"):
        return PaymentActionResult(
            ok=False,
            action="resend",
            diag=diag,
            alert_fa=diag.wrong_tool_alert("resend"),
            error="wrong_tool",
        )
    if order is None and payment.order_id:
        order = await session.get(Order, int(payment.order_id))
    user = await session.get(BotUser, payment.user_id)
    if not user or not order:
        return PaymentActionResult(
            ok=False,
            action="resend",
            diag=diag,
            alert_fa=diag.alert_fa,
            error="missing_user_or_order",
        )
    try:
        await send_delivery_to_user(bot, user.telegram_id, session, payment, order)
        try:
            from app.services.ux20 import resolve_delivery_failure

            await resolve_delivery_failure(session, int(order.id))
            await session.commit()
        except Exception:
            logger.debug("resolve_delivery_failure after resend failed", exc_info=True)
        return PaymentActionResult(
            ok=True,
            action="resend",
            diag=diag,
            order=order,
            alert_fa="پیام تحویل دوباره ارسال شد ✅",
            message_suffix="\n\n📤 ارسال مجدد پیام تحویل",
        )
    except Exception as send_exc:
        try:
            from app.services.ux20 import note_delivery_send_failure

            await note_delivery_send_failure(
                session, order=order, payment=payment, error=str(send_exc)
            )
        except Exception:
            logger.debug("execute_payment_resend: ignored failure", exc_info=True)
        return PaymentActionResult(
            ok=False,
            action="resend",
            diag=diag,
            order=order,
            alert_fa=f"ارسال ناموفق: {user_safe_error(send_exc)}",
            error=str(send_exc),
        )


async def execute_payment_legacy_ok(
    session: AsyncSession,
    payment: Payment,
    *,
    reviewer_tg: int,
    bot: Any,
) -> PaymentActionResult:
    """Back-compat for old inline ``payrev:ok`` buttons — route by diagnosis.

    New markups use ok/go/send explicitly; this keeps unread admin messages usable.
    """
    diag, _ = await _load_diag(session, payment)
    action = diag.preferred_action
    if action == "approve":
        return await execute_payment_approve(
            session, payment, reviewer_tg=reviewer_tg, bot=bot, source="payrev:ok"
        )
    if action == "resume":
        return await execute_payment_resume(
            session,
            payment,
            reviewer_tg=reviewer_tg,
            bot=bot,
            source="payrev:ok:compat-resume",
        )
    if action == "resend":
        return await execute_payment_resend(
            session,
            payment,
            reviewer_tg=reviewer_tg,
            bot=bot,
            source="payrev:ok:compat-resend",
        )
    log_payment_review_diagnosis(
        diag, actor_tg=reviewer_tg, source="payrev:ok:terminal"
    )
    return PaymentActionResult(
        ok=False,
        action="none",
        diag=diag,
        alert_fa=diag.alert_fa,
        error="terminal",
    )


async def _deliver_and_notify(
    session: AsyncSession,
    *,
    bot: Any,
    payment: Payment,
    order: Order | None,
    reviewer_tg: int,
) -> str | None:
    """Send user delivery + staff notify. Returns error string if send failed."""
    user = await session.get(BotUser, payment.user_id)
    if not user:
        return "user_missing"
    try:
        await send_delivery_to_user(bot, user.telegram_id, session, payment, order)
    except Exception as send_exc:
        if order is not None:
            try:
                from app.services.ux20 import note_delivery_send_failure

                await note_delivery_send_failure(
                    session, order=order, payment=payment, error=str(send_exc)
                )
            except Exception:
                logger.debug("_deliver_and_notify: ignored failure", exc_info=True)
        return str(send_exc)
    try:
        from app.services.notifications import notify_new_subscription, notify_wallet_topup_ok

        if payment.is_wallet_topup:
            await notify_wallet_topup_ok(bot, session, payment, user.telegram_id)
        elif order:
            from app.db.models import Plan

            plan = await session.get(Plan, order.plan_id) if order.plan_id else None
            await notify_new_subscription(
                bot,
                session,
                order=order,
                user_tg_id=user.telegram_id,
                user_name=user.full_name or user.username,
                plan_name=plan.name if plan else None,
                needs_approval=False,
            )
    except Exception:
        logger.debug(
            "post-approve notify failed payment=%s reviewer=%s",
            payment.id,
            reviewer_tg,
            exc_info=True,
        )
    return None
