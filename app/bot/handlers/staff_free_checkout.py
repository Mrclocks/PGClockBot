"""«دریافت رایگان (مدیر)» on the payment screen: staff finish their own order without paying."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.chat_cleanup import keep_message
from app.bot.menu_nav import clear_checkout_nav
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, Order, Payment
from app.services import bot_feedback_texts as feedback
from app.services.delivery import send_delivery_to_user
from app.services.formatting import format_message
from app.services.orders import get_plan, is_customer_order_in_current_shop
from app.services.redact import user_safe_error
from app.services.staff_free_checkout import deliver_staff_free_order, may_take_order_free

router = Router(name="staff_free_checkout")
logger = logging.getLogger(__name__)

DELIVERY_UNDER_REVIEW = (
    "پنل به ساخت سرویس پاسخ نداد. سفارش در «تحویل‌های ناموفق» ثبت شد تا وضعیت بررسی شود؛"
    " سفارش دیگری ثبت نکنید."
)
DELIVERY_FAILED_HINT = "سفارش لغو شد؛ پس از رفع مشکل یک خرید تازه ثبت کنید."


async def _notify_stuck(callback: CallbackQuery, session: AsyncSession, payment: Payment, actor: BotUser, error: str):
    # Local import: notifications pulls in handler modules.
    from app.services.notifications import notify_approved_delivery_stuck

    try:
        await notify_approved_delivery_stuck(callback.bot, session, payment, actor.telegram_id, error=error)
    except Exception:
        # The failed-delivery queue still lists the order.
        logger.warning("staff free delivery stuck notice failed payment=%s", payment.id, exc_info=True)


async def _announce_delivery(callback: CallbackQuery, session: AsyncSession, actor: BotUser, order: Order) -> None:
    # Local import: notifications pulls in handler modules.
    from app.services.notifications import notify_new_subscription

    try:
        await send_delivery_to_user(callback.bot, actor.telegram_id, session, None, order)
    except Exception:
        logger.exception("send_delivery_to_user failed order=%s", order.id)
    try:
        plan = await get_plan(session, order.plan_id) if order.plan_id else None
        await notify_new_subscription(
            callback.bot,
            session,
            order=order,
            user_tg_id=actor.telegram_id,
            user_name=actor.full_name or actor.username,
            plan_name=plan.name if plan else None,
            needs_approval=False,
        )
    except Exception:
        logger.exception("notify_new_subscription failed order=%s", order.id)


async def _show(callback: CallbackQuery, title: str, body: str) -> None:
    if not callback.message:
        return
    await safe_edit_text(callback.message, format_message(title, body), reply_markup=kb.back_home(None))
    keep_message(callback.message)


@router.callback_query(F.data.regexp(r"^pay:staff:\d+$"))
async def pay_staff_free_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    order = await session.get(Order, int((callback.data or "").rsplit(":", 1)[-1]))
    if not is_customer_order_in_current_shop(order, db_user.id):
        await callback.answer("این سفارش دیگر در دسترس نیست", show_alert=True)
        return
    allowed = await may_take_order_free(
        session, db_user, order, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not allowed:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        delivered, stuck, error = await deliver_staff_free_order(session, order, actor_id=int(db_user.id))
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    await clear_checkout_nav(state)
    if delivered is not None:
        await _show(callback, "✅ دریافت رایگان ثبت شد", f"سفارش #{delivered.id}")
        await _announce_delivery(callback, session, db_user, delivered)
        return
    logger.warning("staff free delivery failed order=%s", order.id, exc_info=error)
    if stuck is not None:
        await _notify_stuck(callback, session, stuck, db_user, str(error))
        await _show(callback, "⏳ تحویل در حال بررسی", DELIVERY_UNDER_REVIEW)
        return
    await _show(callback, "❌ خطا در تحویل", f"{user_safe_error(error)}\n{DELIVERY_FAILED_HINT}")
