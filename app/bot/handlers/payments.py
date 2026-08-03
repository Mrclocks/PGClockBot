from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.db.models import BotUser, Payment, PaymentMethod, PaymentStatus
from app.services.delivery import send_delivery_to_user
from app.services.formatting import format_message
from app.services.orders import approve_payment, reject_payment, stars_amount_for_toman
from app.services.resellers import reseller_can_review_payment
from app.services.users import get_all_settings, get_setting

router = Router(name="payments")


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
    if int(sp.total_amount or 0) != int(expected_stars):
        await message.answer(
            f"مبلغ استارز نامعتبر است (انتظار {expected_stars}، دریافت {sp.total_amount})."
        )
        return
    payment.receipt_file_id = payment.receipt_file_id or f"stars:{sp.telegram_payment_charge_id}"
    payment.status = PaymentStatus.PENDING.value
    await session.commit()
    try:
        order = await approve_payment(session, payment, reviewer_tg=0)
    except Exception as e:
        await message.answer(f"پرداخت استارز دریافت شد ولی تحویل ناموفق بود: {e}")
        return
    from app.bot.menu_nav import buyer_main_reply_keyboard, clear_checkout_nav

    # Stars success → leave pay menus; delivery attaches main KB
    await clear_checkout_nav(None)
    main_kb, _ = await buyer_main_reply_keyboard(session, db_user, order=order)
    await message.answer(
        format_message("✅ پرداخت استارز", "پرداخت با موفقیت انجام شد."),
        reply_markup=main_kb,
    )
    try:
        await send_delivery_to_user(message.bot, db_user.telegram_id, session, payment, order)
    except Exception:
        try:
            await message.answer("پرداخت شد ولی ارسال جزئیات سرویس ناموفق بود — از «سرویس‌های من» بررسی کنید.")
        except Exception:
            pass
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
        pass


@router.callback_query(F.data.startswith("payrev:ok:"))
async def pay_approve(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    payment_id = int(callback.data.split(":")[-1])
    payment = await session.get(Payment, payment_id)
    if not payment:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if not await reseller_can_review_payment(session, db_user, payment):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        order = await approve_payment(session, payment, db_user.telegram_id)
    except Exception as e:
        await callback.answer(f"خطا: {e}", show_alert=True)
        return
    await callback.answer("تأیید شد ✅")
    if callback.message:
        try:
            if callback.message.photo:
                await callback.message.edit_caption(
                    caption=(callback.message.caption or "") + "\n\n✅ تأیید دستی شد"
                )
            else:
                await callback.message.edit_text(
                    (callback.message.text or "") + "\n\n✅ تأیید دستی شد"
                )
        except Exception:
            pass

    user = await session.get(BotUser, payment.user_id)
    if not user:
        return
    await send_delivery_to_user(callback.bot, user.telegram_id, session, payment, order)
    try:
        from app.services.notifications import notify_new_subscription, notify_wallet_topup_ok

        if payment.is_wallet_topup:
            await notify_wallet_topup_ok(callback.bot, session, payment, user.telegram_id)
        elif order:
            from app.db.models import Plan

            plan = await session.get(Plan, order.plan_id) if order.plan_id else None
            await notify_new_subscription(
                callback.bot,
                session,
                order=order,
                user_tg_id=user.telegram_id,
                user_name=user.full_name or user.username,
                plan_name=plan.name if plan else None,
                needs_approval=False,
            )
    except Exception:
        pass


@router.callback_query(F.data.startswith("payrev:no:"))
async def pay_reject(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    payment_id = int(callback.data.split(":")[-1])
    payment = await session.get(Payment, payment_id)
    if not payment:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if not await reseller_can_review_payment(session, db_user, payment):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await reject_payment(session, payment, db_user.telegram_id, "rejected")
    await callback.answer("رد شد")
    if callback.message:
        try:
            if callback.message.photo:
                await callback.message.edit_caption(
                    caption=(callback.message.caption or "") + "\n\n❌ رد شد"
                )
            else:
                await callback.message.edit_text(
                    (callback.message.text or "") + "\n\n❌ رد شد"
                )
        except Exception:
            pass
    user = await session.get(BotUser, payment.user_id)
    ui = await get_all_settings(session)
    if user:
        reject_body = ui.get("payment_reject_text") or (
            "پرداخت شما رد شد. اگر اشتباهی رخ داده با پشتیبانی در تماس باشید."
        )
        try:
            from app.bot.menu_nav import buyer_main_reply_keyboard

            main_kb, _ = await buyer_main_reply_keyboard(session, user)
            await callback.bot.send_message(
                user.telegram_id,
                format_message("❌ پرداخت رد شد", reject_body),
                reply_markup=main_kb,
                parse_mode="HTML",
            )
        except Exception:
            pass
