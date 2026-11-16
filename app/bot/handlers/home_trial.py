"""«دریافت تست» on the home offer: starts the same trial purchase the shop's trial plan runs.

The checks (terms, contact, account age, one trial per customer and shop) stay in the shop flow
and ``create_order``; this handler only picks the plan and hands over to ``shop_buy``.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.handlers.shop import shop_buy
from app.bot.import_prompt import HOME_TRIAL_CALLBACK
from app.db.models import BotUser
from app.services.formatting import format_message
from app.services.service_scope import list_customer_services
from app.services.trial_offer import offerable_trial_plans
from app.services.users import current_shop_reseller_id, get_all_settings

logger = logging.getLogger(__name__)

router = Router(name="home_trial")

TRIAL_UNAVAILABLE = "پلن تست در دسترس نیست."
TRIAL_TITLE = "🎁 سرویس تست"
TRIAL_PREPARING = "در حال آماده‌سازی سرویس تست…"


async def _remove_offer(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.delete()
    except TelegramAPIError:
        logger.debug("home offer already gone", exc_info=True)


@router.callback_query(F.data == HOME_TRIAL_CALLBACK)
async def home_get_trial(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    reseller_owner_id: int | None = None,
) -> None:
    ui = await get_all_settings(session)
    has_service = await list_customer_services(session, db_user.id, current_shop_reseller_id(), limit=1)
    plans = [] if has_service else await offerable_trial_plans(session, ui, int(db_user.id))
    if not plans:
        await callback.answer(TRIAL_UNAVAILABLE, show_alert=True)
        await _remove_offer(callback)
        return
    if callback.message is None:
        await callback.answer()
        return
    if len(plans) == 1:
        # The purchase edits the message it was tapped on into its result (delivery, payment or an
        # error). Give it a message of its own, so removing the offer cannot remove that result.
        screen = await callback.message.answer(format_message(TRIAL_TITLE, TRIAL_PREPARING))
        buy = callback.model_copy(update={"data": f"shop:buy:{plans[0].id}", "message": screen})
        await shop_buy(buy, session, db_user, state, reseller_owner_id=reseller_owner_id)
    else:
        await callback.answer()
        await callback.message.answer(
            format_message("پلن تست", "پلن تست را انتخاب کنید:"),
            reply_markup=kb.plans_keyboard(plans, ui, back_callback="menu:home", kind="trial"),
        )
    await _remove_offer(callback)
