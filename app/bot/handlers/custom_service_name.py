"""«✏️ نام دلخواه سرویس» on the payment screen."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.menu_nav import PAY_ORDER_ID, present_order_pay, restore_main_reply
from app.bot.nav_input import ask_text, try_legacy_cancel
from app.config import get_settings
from app.db.models import BotUser, Order
from app.services import bot_feedback_texts as feedback
from app.services.checkout_summary import checkout_summary
from app.services.custom_service_name import CLEAR_WORD, NAME_RULES, offers_custom_name, set_service_name
from app.services.orders import is_customer_order_in_current_shop
from app.services.redact import user_safe_error
from app.services.users import get_all_settings

router = Router(name="custom_service_name")

CANCEL_CODE = "pay_name"
ORDER_KEY = "service_name_order_id"
PROMPT = (
    "✏️ <b>نام دلخواه سرویس</b>\n"
    f"نامی که می‌خواهید روی سرویس باشد را بفرستید.\n{NAME_RULES}\n"
    f"برای برگرداندن نام خودکار <code>{CLEAR_WORD}</code> بفرستید."
)


class ServiceNameStates(StatesGroup):
    name = State()


@router.callback_query(F.data.regexp(r"^pay:name:\d+$"))
async def ask_service_name(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
) -> None:
    order = await session.get(Order, int((callback.data or "").rsplit(":", 1)[-1]))
    if not is_customer_order_in_current_shop(order, db_user.id):
        await callback.answer("این سفارش دیگر در دسترس نیست", show_alert=True)
        return
    if not offers_custom_name(await get_all_settings(session), order):
        await callback.answer("انتخاب نام برای این سفارش فعال نیست", show_alert=True)
        return
    await callback.answer()
    await state.update_data(**{ORDER_KEY: int(order.id), PAY_ORDER_ID: int(order.id)})
    await ask_text(
        callback,
        state,
        prompt=PROMPT,
        cancel_code=CANCEL_CODE,
        fsm_state=ServiceNameStates.name,
        edit=True,
    )


async def _back_to_pay(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    order: Order,
    *,
    notice: str,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> None:
    summary = await checkout_summary(session, order, currency=get_settings().currency, notice=notice)
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
    if kb.is_cancel_text(message.text):
        if not await try_legacy_cancel(message, state, session, db_user, **shop):
            await restore_main_reply(message, session, db_user, text=feedback.CANCELLED, state=state, **shop)
        return
    order = await session.get(Order, (await state.get_data()).get(ORDER_KEY))
    if not is_customer_order_in_current_shop(order, db_user.id):
        await state.clear()
        await restore_main_reply(message, session, db_user, text="سفارش معتبر نیست.", state=state, **shop)
        return
    try:
        username = await set_service_name(session, order, buyer_id=int(db_user.id), raw=message.text)
    except ValueError as exc:
        # Stay in this step so the next message is read as a new name.
        await message.answer(html.escape(user_safe_error(exc)))
        return
    await state.set_state(None)
    notice = (
        f"✅ نام سرویس: <code>{html.escape(username)}</code>" if username else "✅ نام خودکار برگشت"
    )
    await _back_to_pay(message, session, db_user, state, order, notice=notice, **shop)
