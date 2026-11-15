"""Answers to the required name step: a typed name or «🎲 اسم تصادفی», then the payment screen."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.menu_nav import present_order_pay, restore_main_reply
from app.bot.service_name_step import ORDER_KEY, ServiceNameStates
from app.config import get_settings
from app.db.models import BotUser, Order
from app.services import bot_feedback_texts as feedback
from app.services.checkout_summary import checkout_summary
from app.services.custom_service_name import offers_custom_name, random_service_name, set_service_name
from app.services.orders import is_customer_order_in_current_shop
from app.services.redact import user_safe_error

router = Router(name="custom_service_name")

ORDER_GONE = "این سفارش دیگر در دسترس نیست"


async def _to_payment(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    order: Order,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> None:
    await state.set_state(None)
    summary = await checkout_summary(session, order, currency=get_settings().currency)
    await present_order_pay(
        message,
        session,
        db_user,
        order.id,
        state=state,
        summary=summary,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data.regexp(r"^pay:namernd:\d+$"))
async def random_service_name_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    order = await session.get(Order, int((callback.data or "").rsplit(":", 1)[-1]))
    if not is_customer_order_in_current_shop(order, db_user.id) or not await offers_custom_name(session, order):
        await callback.answer(ORDER_GONE, show_alert=True)
        return
    try:
        await set_service_name(session, order, buyer_id=int(db_user.id), raw=random_service_name())
    except ValueError as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _to_payment(
            callback.message,
            session,
            db_user,
            state,
            order,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )


@router.message(ServiceNameStates.name)
async def service_name_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop = {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id}
    order_id = (await state.get_data()).get(ORDER_KEY)
    order = await session.get(Order, int(order_id)) if order_id else None
    if kb.is_cancel_text(message.text) or not is_customer_order_in_current_shop(order, db_user.id):
        text = feedback.CANCELLED if order is not None else ORDER_GONE
        await state.clear()
        await restore_main_reply(message, session, db_user, text=text, state=state, **shop)
        return
    try:
        await set_service_name(session, order, buyer_id=int(db_user.id), raw=message.text)
    except ValueError as exc:
        # Stay in this step so the next message is read as a new name.
        await message.answer(html.escape(user_safe_error(exc)))
        return
    await _to_payment(message, session, db_user, state, order, **shop)
