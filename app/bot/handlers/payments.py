from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.pasarguard import failure_text
from app.db.models import BotUser, Order, Payment, PaymentMethod, PaymentStatus
from app.services.chat_records import keep_sent
from app.services.delivery import send_delivery_to_user
from app.services.formatting import format_message
from app.services.notifications import notify_stars_delivery_failed
from app.services.orders import (
    DeliveryAttempt,
    approve_payment,
    hold_unknown_delivery_for_review,
    reject_payment,
    stars_amount_for_toman,
)
from app.services.resellers import reseller_can_review_payment
from app.services.receipt_review import DEFAULT_REJECT_TEXT
from app.services.reject_reasons import (
    NO_REASON_NOTE,
    PAYMENT_REASONS_KEY,
    button_label,
    parse_reasons,
    reason_at,
    with_reason,
)
from app.services.support_button import support_markup
from app.services.users import get_all_settings, get_setting
from app.services import bot_feedback_texts as feedback

logger = logging.getLogger(__name__)

router = Router(name="payments")



async def _refund_stars_charge(bot, *, telegram_user_id: int, charge_id: str) -> bool:
    """Best-effort Telegram Stars refund. Returns True when Telegram accepted it."""
    charge = (charge_id or "").strip()
    if not charge or not telegram_user_id:
        return False
    try:
        return bool(
            await bot.refund_star_payment(
                user_id=int(telegram_user_id),
                telegram_payment_charge_id=charge,
            )
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "stars refund failed user=%s charge=%s", telegram_user_id, charge
        )
        return False


async def _hold_stars_for_review(
    message: Message,
    session: AsyncSession,
    payment: Payment,
    db_user: BotUser,
    *,
    charge_id: str,
    error: Exception,
) -> None:
    """Delivery outcome unknown: keep the Stars with the shop and let staff decide."""
    logger.error(
        "stars delivery outcome unknown payment=%s charge=%s", payment.id, charge_id, exc_info=error
    )
    order = await session.get(Order, payment.order_id) if payment.order_id else None
    if order is not None:
        await hold_unknown_delivery_for_review(session, order, error=failure_text(error))
    with keep_sent():  # the buyer must be able to find this notice again
        await message.answer(
            "پرداخت استارز دریافت شد اما نتیجه تحویل سرویس نیاز به بررسی پشتیبانی دارد.\n"
            "دوباره پرداخت نکنید؛ شناسه پرداخت ثبت شد.",
            reply_markup=support_markup(),
        )
    try:
        await notify_stars_delivery_failed(
            message.bot, session, payment, telegram_id=int(db_user.telegram_id),
            charge_id=charge_id, refunded=False, held=True, error=failure_text(error),
        )
    except Exception:
        # Best-effort staff alert: the order is already listed under failed deliveries.
        logger.warning("stars hold alert failed payment=%s", payment.id, exc_info=True)


@router.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery, session: AsyncSession, db_user: BotUser):
    # Accept Telegram Stars invoices created by this bot — verify payment row
    payload = query.invoice_payload or ""
    if not payload.startswith("stars:"):
        await query.answer(ok=False, error_message="پرداخت نامعتبر")
        return
    parts = payload.split(":")
    if len(parts) < 2:
        await query.answer(ok=False, error_message="پرداخت نامعتبر")
        return
    try:
        payment_id = int(parts[1])
    except ValueError:
        await query.answer(ok=False, error_message="پرداخت نامعتبر")
        return
    payment = await session.get(Payment, payment_id)
    if (
        not payment
        or payment.user_id != db_user.id
        or payment.method != PaymentMethod.STARS.value
        or payment.status not in {PaymentStatus.PENDING.value}
    ):
        await query.answer(ok=False, error_message="پرداخت نامعتبر")
        return
    if len(parts) >= 3:
        try:
            expected = int(parts[2])
            if int(query.total_amount or 0) != expected:
                await query.answer(ok=False, error_message="مبلغ نامعتبر")
                return
        except ValueError:
            await query.answer(ok=False, error_message="پرداخت نامعتبر")
            return
    await query.answer(ok=True)

@router.message(F.successful_payment)
async def stars_successful_payment(message: Message, session: AsyncSession, db_user: BotUser):
    sp = message.successful_payment
    if not sp or sp.currency != "XTR":
        return
    payload = sp.invoice_payload or ""
    if not payload.startswith("stars:"):
        return
    parts = payload.split(":")
    if len(parts) < 2:
        return
    try:
        payment_id = int(parts[1])
    except ValueError:
        return
    payment = await session.get(Payment, payment_id)
    if not payment or payment.user_id != db_user.id:
        await message.answer("پرداخت یافت نشد.")
        return
    if payment.method != PaymentMethod.STARS.value:
        return
    if payment.status == PaymentStatus.APPROVED.value:
        await message.answer("این پرداخت قبلاً تأیید شده.")
        return
    expected_stars = None
    if len(parts) >= 3:
        try:
            expected_stars = int(parts[2])
        except ValueError:
            expected_stars = None
    if expected_stars is None:
        try:
            rate = int(float(await get_setting(session, "stars_toman_per_star") or 500))
        except (TypeError, ValueError):
            rate = 500
        expected_stars = stars_amount_for_toman(payment.amount, rate)
    charge_id = (sp.telegram_payment_charge_id or "").strip()
    if int(sp.total_amount or 0) != int(expected_stars):
        refunded = await _refund_stars_charge(
            message.bot,
            telegram_user_id=int(db_user.telegram_id),
            charge_id=charge_id,
        )
        payment.receipt_file_id = payment.receipt_file_id or (
            f"stars:{charge_id}" if charge_id else payment.receipt_file_id
        )
        payment.status = PaymentStatus.REJECTED.value
        payment.review_note = (
            f"stars amount mismatch expected={expected_stars} got={sp.total_amount}"
            + ("; refunded" if refunded else "; refund_failed")
        )
        await session.commit()
        if refunded:
            await message.answer(
                f"مبلغ استارز نامعتبر بود (انتظار {expected_stars}، دریافت {sp.total_amount}). "
                "استارز به‌صورت خودکار بازگردانده شد."
            )
        else:
            await message.answer(
                f"مبلغ استارز نامعتبر است (انتظار {expected_stars}، دریافت {sp.total_amount}). "
                "بازگشت خودکار ناموفق بود — با پشتیبانی تماس بگیرید و شناسه پرداخت را بفرستید.",
                reply_markup=support_markup(),
            )
        return
    payment.receipt_file_id = payment.receipt_file_id or f"stars:{charge_id}"
    payment.status = PaymentStatus.PENDING.value
    await session.commit()
    attempt = DeliveryAttempt()
    try:
        order = await approve_payment(session, payment, reviewer_tg=0, attempt=attempt)
    except Exception as e:
        if attempt.panel_may_hold_account:
            await _hold_stars_for_review(
                message, session, payment, db_user, charge_id=charge_id, error=e
            )
            return
        logging.getLogger(__name__).error(
            "stars delivery failed payment=%s charge=%s err=%s",
            payment.id,
            charge_id,
            e,
            exc_info=True,
        )
        refunded = await _refund_stars_charge(
            message.bot,
            telegram_user_id=int(db_user.telegram_id),
            charge_id=charge_id,
        )
        # approve_payment may have left the row APPROVED or rolled it; force a
        # clear rejected marker when we refund so staff do not re-deliver.
        try:
            await session.refresh(payment)
            note = (
                f"stars approve/delivery failed: {e}"
                + ("; refunded" if refunded else "; refund_failed")
            )
            # Refunded → reject so staff do not re-deliver. Still-APPROVED without
            # refund stays approved for manual retry, but always stamp the note.
            if refunded or payment.status != PaymentStatus.APPROVED.value:
                payment.status = PaymentStatus.REJECTED.value
            payment.review_note = payment.review_note or note
            await session.commit()
        except Exception:
            logging.getLogger(__name__).exception(
                "stars post-failure payment update failed id=%s", payment.id
            )
        with keep_sent():  # a payment outcome the buyer must be able to find again
            if refunded:
                await message.answer("پرداخت استارز دریافت شد ولی تحویل ناموفق بود و استارز بازگردانده شد.")
            else:
                await message.answer(
                    "پرداخت استارز دریافت شد ولی تحویل ناموفق بود.\n"
                    "بازگشت خودکار ناموفق بود — با پشتیبانی تماس بگیرید؛ شناسه پرداخت ثبت شد.",
                    reply_markup=support_markup(),
                )
        try:
            await notify_stars_delivery_failed(
                message.bot, session, payment, telegram_id=int(db_user.telegram_id),
                charge_id=charge_id, refunded=refunded, error=failure_text(e),
            )
        except Exception:
            # Best-effort staff alert: the failure is logged above and stamped on the payment.
            logging.getLogger(__name__).warning("stars failure alert failed payment=%s", payment.id, exc_info=True)
        return
    from app.bot.menu_nav import buyer_main_reply_keyboard, clear_checkout_nav

    # Stars success → leave pay menus; delivery attaches main KB
    await clear_checkout_nav(None)
    main_kb, _ = await buyer_main_reply_keyboard(
        session,
        db_user,
        order=order,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )
    with keep_sent():
        await message.answer(
            format_message("✅ پرداخت استارز", "پرداخت با موفقیت انجام شد."),
            reply_markup=main_kb,
        )
    try:
        await send_delivery_to_user(message.bot, db_user.telegram_id, session, payment, order)
    except Exception as send_exc:
        if order is not None:
            try:
                from app.services.delivery_failures import note_delivery_send_failure

                await note_delivery_send_failure(
                    session, order=order, payment=payment, error=str(send_exc)
                )
            except Exception:
                logger.debug("stars_successful_payment: ignored failure", exc_info=True)
        try:
            with keep_sent():
                await message.answer("پرداخت شد ولی ارسال جزئیات سرویس ناموفق بود — از «سرویس‌های من» بررسی کنید.")
        except Exception:
            logger.debug("stars_successful_payment: ignored failure", exc_info=True)
    try:
        from app.db.models import Plan
        from app.services.notifications import notify_new_subscription

        if order:
            plan = await session.get(Plan, order.plan_id) if order.plan_id else None
            await notify_new_subscription(
                message.bot,
                session,
                order=order,
                user_tg_id=db_user.telegram_id,
                user_name=db_user.full_name or db_user.username,
                plan_name=plan.name if plan else None,
                needs_approval=False,
            )
    except Exception:
        logger.debug("stars_successful_payment: ignored failure", exc_info=True)


async def _payrev_load(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, *, payment_id: int | None = None
) -> Payment | None:
    if payment_id is None:
        payment_id = int(callback.data.split(":")[-1])
    payment = await session.get(Payment, payment_id)
    if not payment:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return None
    if not await reseller_can_review_payment(session, db_user, payment):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return None
    if callback.message and callback.data.split(":")[1] in {"ok", "no"}:
        from app.services.payment_review_messages import remember_review_message
        from app.services.orders import _sync_payment_review_messages

        await remember_review_message(session, payment_id, callback.bot, callback.message)
        await session.commit()
        await _sync_payment_review_messages(session, payment_id, bot=callback.bot)
    return payment


async def _payrev_finish(callback: CallbackQuery, result) -> None:
    await callback.answer(result.alert_fa, show_alert=True)
    # Approval cards are edited together from their stored references. Recovery
    # cards still finish locally after resume/resend, preserving their old flow.
    if (
        callback.data.split(":")[1] not in {"go", "send"}
        or not result.ok or not result.message_suffix or not callback.message
    ):
        return
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.html_caption or "") + result.message_suffix,
                reply_markup=None,
            )
        else:
            await callback.message.edit_text(
                (callback.message.html_text or "") + result.message_suffix,
                reply_markup=None,
            )
    except Exception:
        logger.debug("_payrev_finish: ignored failure", exc_info=True)


@router.callback_query(F.data.startswith("payrev:ok:"))
async def pay_approve(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Legacy + pending approve. Routes by diagnosis for old inline buttons."""
    payment = await _payrev_load(callback, session, db_user)
    if not payment:
        return
    from app.services.payment_review_actions import execute_payment_legacy_ok

    result = await execute_payment_legacy_ok(
        session,
        payment,
        reviewer_tg=int(db_user.telegram_id),
        bot=callback.bot,
    )
    await _payrev_finish(callback, result)


@router.callback_query(F.data.startswith("payrev:go:"))
async def pay_resume(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Phase 1: resume incomplete APPROVED fulfill/credit only."""
    payment = await _payrev_load(callback, session, db_user)
    if not payment:
        return
    from app.services.payment_review_actions import execute_payment_resume

    result = await execute_payment_resume(
        session,
        payment,
        reviewer_tg=int(db_user.telegram_id),
        bot=callback.bot,
    )
    await _payrev_finish(callback, result)


@router.callback_query(F.data.startswith("payrev:send:"))
async def pay_resend(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Phase 1: Telegram re-send only (no mint / no wallet credit)."""
    payment = await _payrev_load(callback, session, db_user)
    if not payment:
        return
    from app.services.payment_review_actions import execute_payment_resend

    result = await execute_payment_resend(
        session,
        payment,
        reviewer_tg=int(db_user.telegram_id),
        bot=callback.bot,
    )
    await _payrev_finish(callback, result)


@router.callback_query(F.data.startswith("payrev:no:"))
async def pay_reject(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Offer the shop's ready-made reasons; without any, reject at once as before."""
    payment = await _payrev_load(callback, session, db_user)
    if not payment:
        return
    reasons = parse_reasons((await get_all_settings(session)).get(PAYMENT_REASONS_KEY))
    if not reasons or not callback.message:
        await _reject_with_reason(callback, session, db_user, payment, None)
        return
    await callback.answer()
    with keep_sent():  # the picker is removed by its own buttons, not by chat cleanup
        await callback.message.answer(
            f"دلیل رد پرداخت #{int(payment.id)} را انتخاب کنید (برای مشتری فرستاده می‌شود):",
            reply_markup=_reject_reason_markup(int(payment.id), reasons),
        )


def _reject_reason_markup(payment_id: int, reasons: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=button_label(reason), callback_data=f"payrev:nr:{payment_id}:{number}")]
        for number, reason in enumerate(reasons, start=1)
    ]
    rows.append([InlineKeyboardButton(text="رد بدون دلیل", callback_data=f"payrev:nr:{payment_id}:0")])
    rows.append([InlineKeyboardButton(text="↩️ رد نکن", callback_data=f"payrev:nx:{payment_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^payrev:nr:(\d{1,12}):([0-8])$"))
async def pay_reject_with_reason(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    _, _, raw_id, raw_number = callback.data.split(":")
    payment = await _payrev_load(callback, session, db_user, payment_id=int(raw_id))
    if not payment:
        return
    number = int(raw_number)
    reason: str | None = None
    if number:
        reasons = parse_reasons((await get_all_settings(session)).get(PAYMENT_REASONS_KEY))
        try:
            reason = reason_at(reasons, number)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    if await _reject_with_reason(callback, session, db_user, payment, reason):
        await _drop_picker(callback)


@router.callback_query(F.data.regexp(r"^payrev:nx:\d{1,12}$"))
async def pay_reject_dismiss(callback: CallbackQuery):
    await callback.answer()
    await _drop_picker(callback)


async def _drop_picker(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.debug("reject reason picker: delete failed", exc_info=True)


async def _reject_with_reason(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, payment: Payment, reason: str | None
) -> bool:
    """Reject once (the claim in ``reject_payment``) and tell the payer, with the reason when one was chosen."""
    try:
        await reject_payment(
            session, payment, db_user.telegram_id, reason or NO_REASON_NOTE, bot=callback.bot
        )
    except ValueError as exc:
        from app.services.redact import user_safe_error

        await callback.answer(user_safe_error(exc), show_alert=True)
        return False
    await callback.answer(feedback.REJECTED)
    user = await session.get(BotUser, payment.user_id)
    ui = await get_all_settings(session)
    if user:
        from app.services.rich_text import outbound_setting_text

        reject_body = ui.get("payment_reject_text") or DEFAULT_REJECT_TEXT
        text, send_kw = outbound_setting_text(
            reject_body, title="❌ پرداخت رد شد"
        )
        text = with_reason(text, send_kw, reason)
        try:
            # An inline button under the notice; the buyer's main keyboard stays as it is.
            with keep_sent():  # a notice or review item, never a temporary panel
                await callback.bot.send_message(
                    user.telegram_id,
                    text,
                    reply_markup=support_markup(),
                    **{"parse_mode": "HTML", **send_kw},
                )
        except Exception:
            logger.debug("pay_reject: ignored failure", exc_info=True)
    return True
