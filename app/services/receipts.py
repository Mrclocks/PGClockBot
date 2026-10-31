from __future__ import annotations

"""Receipt submission: manual admin approve or optional auto-approve."""

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment
from app.services.delivery import send_delivery_to_user
from app.services.formatting import format_message
from app.services.notifications import (
    notify_approved_delivery_stuck,
    notify_auto_approve,
    notify_new_subscription,
    notify_pending_approval,
    notify_wallet_topup_ok,
)
from app.services.orders import approve_payment, attach_receipt
from app.services.receipt_fingerprints import (
    download_receipt_sha256,
    find_duplicate_receipts,
    format_dup_warning,
    get_receipt_dup_policy,
    store_receipt_fingerprint,
)
from app.services.users import get_setting, on
import logging

logger = logging.getLogger(__name__)


async def submit_receipt(
    session: AsyncSession,
    payment: Payment,
    *,
    bot: Bot,
    file_id: str,
    file_unique_id: str | None = None,
    user_tg_id: int | None = None,
    first_only: bool = False,
) -> str | None:
    """Attach receipt, record fingerprints, then process (auto-approve or notify).

    ``first_only`` is passed to ``attach_receipt``: the payment must not have a receipt yet.

    Duplicate policy (``receipt_dup_policy``):
    - ``warn`` (default): still accept; warn reviewers with earlier payment ids
    - ``block``: reject upload when fingerprint matches another payment
    """
    shop_rid = None
    if payment.is_wallet_topup:
        wid = getattr(payment, "wallet_shop_id", None)
        shop_rid = int(wid) if wid else None
    elif payment.order_id:
        from app.db.models import Order

        ord_row = await session.get(Order, payment.order_id)
        if ord_row and ord_row.reseller_id:
            shop_rid = int(ord_row.reseller_id)

    sha = await download_receipt_sha256(bot, file_id)
    matches = await find_duplicate_receipts(
        session,
        file_unique_id=file_unique_id,
        sha256=sha,
        exclude_payment_id=int(payment.id),
    )
    policy = await get_receipt_dup_policy(session, reseller_id=shop_rid)
    if matches and policy == "block":
        warn = format_dup_warning(matches)
        return format_message(
            "⛔ رسید تکراری",
            (warn + "\n\n" if warn else "")
            + "این تصویر قبلاً برای پرداخت دیگری ثبت شده و طبق تنظیمات فروشگاه رد شد.\n"
            "اگر اشتباه است با پشتیبانی تماس بگیرید.",
        )

    await attach_receipt(session, payment, file_id, first_only=first_only)
    try:
        await store_receipt_fingerprint(
            session,
            payment_id=int(payment.id),
            file_unique_id=file_unique_id,
            sha256=sha,
            commit=True,
        )
    except Exception:
        # Fingerprint is best-effort; payment attachment must stay durable.
        pass

    dup_note = format_dup_warning(matches) if matches else None
    return await process_receipt(
        session,
        payment,
        bot=bot,
        user_tg_id=user_tg_id,
        reviewer_note=dup_note,
    )


async def process_receipt(
    session: AsyncSession,
    payment: Payment,
    *,
    bot: Bot,
    user_tg_id: int | None,
    reviewer_note: str | None = None,
) -> str | None:
    """
    After receipt is attached:
    - if auto_approve_payments=1 → approve, deliver (+QR), return None (already sent)
    - else → notify admins; return status text for the user
    """
    # Auto-approve flag is shop-scoped for the payment's tenant.
    shop_rid = None
    if payment.is_wallet_topup:
        wid = getattr(payment, "wallet_shop_id", None)
        shop_rid = int(wid) if wid else None
    elif payment.order_id:
        from app.db.models import Order

        ord_row = await session.get(Order, payment.order_id)
        if ord_row and ord_row.reseller_id:
            shop_rid = int(ord_row.reseller_id)
    auto = on(
        await get_setting(
            session,
            "auto_approve_payments",
            "0",
            reseller_id=shop_rid,
        )
    )
    # Top-ups may auto-approve only inside their own purse scope (shop→shop
    # wallet, platform→platform). Cross-scope would re-open minting.
    if auto and payment.is_wallet_topup:
        topup_shop = (
            int(payment.wallet_shop_id)
            if getattr(payment, "wallet_shop_id", None)
            else None
        )
        if topup_shop != shop_rid:
            auto = False
    if auto:
        order = None
        try:
            order = await approve_payment(session, payment, reviewer_tg=0)
            if user_tg_id:
                try:
                    await send_delivery_to_user(bot, user_tg_id, session, payment, order)
                except Exception as send_exc:
                    if order is not None:
                        from app.services.ux20 import note_delivery_send_failure

                        await note_delivery_send_failure(
                            session, order=order, payment=payment, error=str(send_exc)
                        )
                    raise
            await notify_auto_approve(bot, session, payment)
            if payment.is_wallet_topup:
                await notify_wallet_topup_ok(bot, session, payment, user_tg_id)
            elif order:
                plan_name = None
                if order.plan_id:
                    from app.db.models import Plan

                    plan = await session.get(Plan, order.plan_id)
                    plan_name = plan.name if plan else None
                await notify_new_subscription(
                    bot,
                    session,
                    order=order,
                    user_tg_id=user_tg_id,
                    plan_name=plan_name,
                    needs_approval=False,
                )
            return None
        except Exception as e:
            # Re-load payment — approve may have committed APPROVED before delivery failed.
            # Never send pending-approval «رد» chrome for an already-approved row.
            try:
                await session.refresh(payment)
            except Exception:
                logger.debug("process_receipt: ignored failure", exc_info=True)
            from app.db.models import PaymentStatus

            if payment.status == PaymentStatus.APPROVED.value:
                stuck_order = order
                if stuck_order is None and payment.order_id:
                    from app.db.models import Order

                    stuck_order = await session.get(Order, payment.order_id)
                if stuck_order is not None:
                    try:
                        from app.services.ux20 import note_delivery_send_failure

                        await note_delivery_send_failure(
                            session,
                            order=stuck_order,
                            payment=payment,
                            error=str(e),
                        )
                    except Exception:
                        logger.debug("process_receipt: ignored failure", exc_info=True)
                await notify_approved_delivery_stuck(
                    bot, session, payment, user_tg_id, error=str(e)
                )
                return format_message(
                    "⚠️ رسید ثبت شد",
                    "پرداخت تأیید شد ولی تحویل کامل نشد.\n"
                    "ادمین می‌تواند «تلاش مجدد تحویل» را بزند یا از «تحویل ناموفق» پیگیری کند.",
                )
            await notify_pending_approval(
                bot, session, payment, user_tg_id, extra_note=reviewer_note
            )
            return format_message(
                "⚠️ رسید ثبت شد",
                f"تأیید خودکار ناموفق بود ({e}).\nمنتظر تأیید دستی ادمین بمانید.",
            )

    await notify_pending_approval(
        bot, session, payment, user_tg_id, extra_note=reviewer_note
    )
    return format_message(
        "✅ رسید دریافت شد",
        "رسید شما ثبت شد.\nپس از تأیید ادمین، سرویس/شارژ فعال می‌شود.",
    )
