"""A customer cancels their own unpaid purchase: «انصراف» on the payment methods, or «لغو این خرید» on an order card.

Leaving the payment screen used to keep the order pending, and with a random discount every such order held one of
the customer's few reserved amounts. «انصراف» now cancels the order when nothing has been paid (otherwise it only
goes home). On an order card in «🧾 سفارش‌های من» the cancel asks for a confirmation first. The service
(``checkout_cancel``) scopes the order by the customer and the open shop and refuses anything with money moving.
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.customer_orders import LIST_CALLBACK
from app.bot.handlers.start import render_home
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.checkout_cancel import cancel_customer_checkout
from app.services.users import current_shop_reseller_id, get_all_settings

router = Router(name="checkout_cancel")

ID = r"(\d{1,12})"
PAY_CANCEL_PREFIX = "pay:x:"
CARD_CANCEL_PREFIX = "myord:x:"
CARD_CONFIRM_PREFIX = "myord:xy:"
CANCELLED = "خرید لغو شد."
CONFIRM_TEXT = (
    "❌ <b>لغو خرید #{order_id}</b>\n"
    "این سفارش لغو می‌شود. اگر مبلغ را کارت‌به‌کارت واریز کرده‌اید ولی رسیدش را نفرستاده‌اید، لغو نکنید و رسید را "
    "بفرستید."
)


def _order_id(data: str | None, prefix: str) -> int:
    return int((data or "")[len(prefix):])


async def _cancel(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, order_id: int) -> str | None:
    """Cancel the order; the safe reason when it cannot be cancelled."""
    try:
        await cancel_customer_checkout(
            session, user_id=int(db_user.id), order_id=order_id, shop_id=current_shop_reseller_id()
        )
    except ValueError as exc:
        return str(exc)
    return None


@router.callback_query(F.data.regexp(rf"^pay:x:{ID}$"))
async def cancel_from_payment(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """«انصراف» on the payment methods: cancel what nothing was paid for, then the home menu either way."""
    refused = await _cancel(callback, session, db_user, _order_id(callback.data, PAY_CANCEL_PREFIX))
    await callback.answer(refused or CANCELLED, show_alert=bool(refused))
    await state.clear()
    if callback.message:
        await render_home(
            callback.message, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
        )


@router.callback_query(F.data.regexp(rf"^myord:x:{ID}$"))
async def ask_to_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    order_id = _order_id(callback.data, CARD_CANCEL_PREFIX)
    await callback.answer()
    rows = [[InlineKeyboardButton(text="✅ بله، لغو شود", callback_data=f"{CARD_CONFIRM_PREFIX}{order_id}")]]
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), f"myord:v:{order_id}")
    if callback.message:
        await safe_edit_text(callback.message, CONFIRM_TEXT.format(order_id=order_id), reply_markup=markup)


@router.callback_query(F.data.regexp(rf"^myord:xy:{ID}$"))
async def confirm_cancel(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    refused = await _cancel(callback, session, db_user, _order_id(callback.data, CARD_CONFIRM_PREFIX))
    if refused:
        await callback.answer(refused, show_alert=True)
        return
    await callback.answer(CANCELLED)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=[]), await get_all_settings(session), LIST_CALLBACK)
    if callback.message:
        await safe_edit_text(callback.message, f"✅ {html.escape(CANCELLED)}", reply_markup=markup)
