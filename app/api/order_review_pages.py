"""Order and payment review routes of the web panel: approve, reject and cancel orders and payments.

Registered from ``create_api_app`` at the point its routes always had, so route order is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentStatus, Plan
from app.services.orders import (
    approve_payment,
    cancel_order,
    fulfill_paid_order,
    is_mutation_order_note,
    manual_fulfill_unpaid_order,
    reject_order,
    reject_payment,
)
from app.services.payer_notify import notify_payer
from app.services.receipt_review import DEFAULT_REJECT_TEXT
from app.services.redact import operator_error
from app.services.shop_scope import payment_shop_id
from app.services.staff_audit import actor_from_staff
from app.services.support_button import support_markup
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)


def register_order_review_pages(  # noqa: C901 - counts the nested routes; each is listed on its own
    app: object,
    *,
    get_db: Callable[..., object],
    redirect_msg: Callable[..., object],
    require_perm: Callable[..., object],
) -> None:
    async def _notify_order_user(session: AsyncSession, payment: Payment, order: Order | None) -> None:
        try:
            from app.services.payment_settlement import notify_after_settlement

            await notify_after_settlement(session, payment, order)
        except Exception:
            logger.debug("_notify_order_user: ignored failure", exc_info=True)

    @app.post("/orders/{order_id}/approve")
    async def order_approve(  # noqa: C901 - moved unchanged from app.py, where it had this finding
        order_id: int,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        order = await session.get(Order, order_id)
        if not order:
            return redirect_msg("/finance?tab=orders", err="سفارش یافت نشد")
        # Hard shop isolation (match bot ordrev + web payments): platform admin
        # must never approve tenant shop orders.
        if order.reseller_id and staff.get("role") == "admin":
            return redirect_msg(
                "/finance?tab=orders",
                err="این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل تأیید است",
            )
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, assert_order_in_scope

            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError as e:
                return redirect_msg("/finance?tab=orders", err=e.message)
        if order.status == OrderStatus.DELIVERED.value:
            return redirect_msg("/finance?tab=orders", ok="قبلاً تحویل شده")
        # Already provisioned (status may have been tampered) — never re-deliver/notify.
        # Mutation orders (renew / reseller_app / svc_addon) keep service_id as the
        # target to mutate — do not treat them as already-minted deliveries.
        note = (order.note or "").strip()
        if order.service_id and not is_mutation_order_note(note):
            if order.status != OrderStatus.DELIVERED.value:
                order.status = OrderStatus.DELIVERED.value
                await session.commit()
            return redirect_msg("/finance?tab=orders", ok="قبلاً تحویل شده")

        result = await session.execute(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        payment = result.scalar_one_or_none()
        try:
            if payment and payment.status == PaymentStatus.PENDING.value:
                # Approving a pending receipt requires payments perm (not only orders)
                if staff.get("role") != "admin":
                    perms = staff.get("permissions") or []
                    if "payments" not in perms:
                        return redirect_msg(
                "/finance?tab=orders",
                            err="تأیید رسید نیاز به دسترسی «پرداخت‌ها» دارد",
                        )
                delivered = await approve_payment(session, payment, reviewer_tg=0, actor=actor_from_staff(staff, shop_id=None))
                await _notify_order_user(session, payment, delivered or order)
            elif order.status == OrderStatus.PAID.value:
                had_service = bool(order.service_id)
                was_mutation = is_mutation_order_note(note)
                delivered = await fulfill_paid_order(session, order)
                if payment and (was_mutation or not had_service):
                    await _notify_order_user(session, payment, delivered)
            elif payment and payment.status == PaymentStatus.APPROVED.value and order.status != OrderStatus.DELIVERED.value:
                had_service = bool(order.service_id)
                was_mutation = is_mutation_order_note(note)
                delivered = await fulfill_paid_order(session, order)
                if was_mutation or not had_service:
                    await _notify_order_user(session, payment, delivered)
            elif order.status in {
                OrderStatus.PENDING.value,
                OrderStatus.AWAITING_RECEIPT.value,
            }:
                # Pending without receipt: staff can still approve → deliver once
                delivered, payment = await manual_fulfill_unpaid_order(
                    session, order, note="web manual approve"
                )
                await _notify_order_user(session, payment, delivered)
            else:
                return redirect_msg("/finance?tab=orders", err="این سفارش هنوز قابل تأیید نیست (رسید لازم است)")
        except Exception as e:
            return redirect_msg("/finance?tab=orders", err=operator_error(e))
        return redirect_msg("/finance?tab=orders", ok="سفارش تأیید و تحویل شد")

    @app.post("/orders/{order_id}/reject")
    async def order_reject(
        order_id: int,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        order = await session.get(Order, order_id)
        if not order:
            return redirect_msg("/finance?tab=orders", err="سفارش یافت نشد")
        if order.reseller_id and staff.get("role") == "admin":
            return redirect_msg(
                "/finance?tab=orders",
                err="این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل رد است",
            )
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, assert_order_in_scope

            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError as e:
                return redirect_msg("/finance?tab=orders", err=e.message)
        # Rejecting a pending receipt also requires payments perm for limited staff
        pending_pay = (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == order_id, Payment.status == PaymentStatus.PENDING.value)
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending_pay and staff.get("role") != "admin":
            perms = staff.get("permissions") or []
            if "payments" not in perms:
                return redirect_msg(
                "/finance?tab=orders",
                    err="رد رسید نیاز به دسترسی «پرداخت‌ها» دارد",
                )
        try:
            if pending_pay:
                await reject_payment(
                    session, pending_pay, reviewer_tg=0, note="web order reject", actor=actor_from_staff(staff, shop_id=None)
                )
            else:
                await reject_order(session, order, note="web order reject")
        except Exception as e:
            return redirect_msg("/finance?tab=orders", err=operator_error(e))
        await notify_payer(
            session,
            order.user_id,
            title="❌ سفارش رد شد",
            body=f"سفارش #{order_id} رد شد. اگر اشتباهی رخ داده با پشتیبانی در تماس باشید.",
            shop_id=order.reseller_id,
            reply_markup=support_markup(),
        )
        return redirect_msg("/finance?tab=orders", ok="سفارش رد شد")

    @app.post("/orders/{order_id}/cancel")
    async def order_cancel(
        order_id: int,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        order = await session.get(Order, order_id)
        if not order:
            return redirect_msg("/finance?tab=orders", err="سفارش یافت نشد")
        if order.reseller_id and staff.get("role") == "admin":
            return redirect_msg(
                "/finance?tab=orders",
                err="این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل لغو است",
            )
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, assert_order_in_scope

            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError as e:
                return redirect_msg("/finance?tab=orders", err=e.message)
        try:
            await cancel_order(session, order, note="web order cancel")
        except Exception as e:
            return redirect_msg("/finance?tab=orders", err=operator_error(e))
        await notify_payer(
            session,
            order.user_id,
            title="⏹ سفارش لغو شد",
            body=f"سفارش #{order_id} لغو شد.",
            shop_id=order.reseller_id,
            reply_markup=support_markup(),
        )
        return redirect_msg("/finance?tab=orders", ok="سفارش لغو شد")


    @app.get("/payments/{payment_id}/approve")
    async def payment_approve_get(payment_id: int):
        return redirect_msg("/finance?tab=payments", err="برای تأیید از دکمه داخل صفحه پرداخت‌ها استفاده کنید")

    @app.post("/payments/{payment_id}/approve")
    async def payment_approve(
        payment_id: int,
        staff: dict = Depends(require_perm("payments")),
        session: AsyncSession = Depends(get_db),
    ):
        payment = await session.get(Payment, payment_id)
        if not payment:
            return redirect_msg("/finance?tab=payments", err="پرداخت یافت نشد")
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, require_shop_owner_id, shop_may_review_payment

            try:
                rid = require_shop_owner_id(staff)
            except ShopScopeError as e:
                return redirect_msg("/finance?tab=payments", err=e.message)
            order_row = await session.get(Order, payment.order_id) if payment.order_id else None
            if not shop_may_review_payment(payment, order_row, rid):
                return redirect_msg("/finance?tab=payments", err="دسترسی به این پرداخت ندارید")
        else:
            # Platform admin must not approve shop-tenant payments or shop wallet top-ups.
            from app.services.shop_scope import platform_may_review_payment

            order_row = await session.get(Order, payment.order_id) if payment.order_id else None
            if not platform_may_review_payment(payment, order_row):
                return redirect_msg(
                    "/finance?tab=payments",
                    err=(
                        "شارژ کیف پول فروشگاه فقط توسط نماینده همان فروشگاه تأیید می‌شود"
                        if payment.is_wallet_topup
                        else "پرداخت‌های فروشگاه فقط توسط نماینده همان فروشگاه تأیید می‌شود"
                    ),
                )
        if payment.status != PaymentStatus.PENDING.value:
            return redirect_msg("/finance?tab=payments", err="این پرداخت قابل تأیید نیست")
        try:
            order = await approve_payment(session, payment, reviewer_tg=0, actor=actor_from_staff(staff, shop_id=None))
        except Exception as e:
            return redirect_msg("/finance?tab=payments", err=operator_error(e))
        user = await session.get(BotUser, payment.user_id)
        if user:
            try:
                from app.services.delivery import send_delivery_to_user
                from app.services.notifications import notify_new_subscription, notify_wallet_topup_ok
                from app.services.reseller_bots import open_notify_bot_for_user

                bot, should_close = await open_notify_bot_for_user(
                    session, user, shop_id=payment_shop_id(payment, order)
                )
                try:
                    try:
                        await send_delivery_to_user(
                            bot, user.telegram_id, session, payment, order
                        )
                    except Exception as send_exc:
                        if order is not None:
                            from app.services.ux20 import note_delivery_send_failure

                            await note_delivery_send_failure(
                                session, order=order, payment=payment, error=str(send_exc)
                            )
                        raise
                    if payment.is_wallet_topup:
                        await notify_wallet_topup_ok(bot, session, payment, user.telegram_id)
                    elif order:
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
                finally:
                    if should_close:
                        await bot.session.close()
            except Exception:
                logger.debug("payment_approve: ignored failure", exc_info=True)
        return redirect_msg("/finance?tab=payments", ok="پرداخت تأیید شد")

    @app.post("/payments/{payment_id}/reject")
    async def payment_reject(
        payment_id: int,
        staff: dict = Depends(require_perm("payments")),
        session: AsyncSession = Depends(get_db),
    ):
        payment = await session.get(Payment, payment_id)
        if not payment:
            return redirect_msg("/finance?tab=payments", err="پرداخت یافت نشد")
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, require_shop_owner_id, shop_may_review_payment

            try:
                rid = require_shop_owner_id(staff)
            except ShopScopeError as e:
                return redirect_msg("/finance?tab=payments", err=e.message)
            order_row = await session.get(Order, payment.order_id) if payment.order_id else None
            if not shop_may_review_payment(payment, order_row, rid):
                return redirect_msg("/finance?tab=payments", err="دسترسی به این پرداخت ندارید")
        else:
            from app.services.shop_scope import platform_may_review_payment

            order_row = await session.get(Order, payment.order_id) if payment.order_id else None
            if not platform_may_review_payment(payment, order_row):
                return redirect_msg(
                    "/finance?tab=payments",
                    err=(
                        "شارژ کیف پول فروشگاه فقط توسط نماینده همان فروشگاه رد می‌شود"
                        if payment.is_wallet_topup
                        else "پرداخت‌های فروشگاه فقط توسط نماینده همان فروشگاه رد می‌شود"
                    ),
                )
        try:
            await reject_payment(session, payment, reviewer_tg=0, note="web reject", actor=actor_from_staff(staff, shop_id=None))
        except Exception as e:
            return redirect_msg("/finance?tab=payments", err=operator_error(e))
        reject_order = await session.get(Order, payment.order_id) if payment.order_id else None
        shop_rid = payment_shop_id(payment, reject_order)
        ui = await get_all_settings(session, reseller_id=shop_rid)
        # The shop's own (possibly rich-text) rejection text, built the same way as the bot's reject handler.
        from app.services.rich_text import outbound_setting_text

        text, send_kw = outbound_setting_text(
            ui.get("payment_reject_text") or DEFAULT_REJECT_TEXT, title="❌ پرداخت رد شد"
        )
        await notify_payer(
            session, payment.user_id, title="❌ پرداخت رد شد", body=text, send_kw=send_kw, shop_id=shop_rid,
            reply_markup=support_markup(),
        )
        return redirect_msg("/finance?tab=payments", ok="پرداخت رد شد")
