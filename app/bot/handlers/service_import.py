"""Customer flow: add a service created outside the bot by sending its subscription link.

The link is a bearer secret, so the message that carries it is deleted as soon as it is read,
every refusal uses the same generic text, and the checks live in ``services.service_import``.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import (
    CancelEntry,
    ask_text,
    cancel_keyboard,
    delete_user_message,
    finish_text_step,
    register_cancel_code,
    try_legacy_cancel,
)
from app.bot.import_prompt import HOME_ADD_CALLBACK
from app.bot.service_import_ui import ADD_CALLBACK, announce_new_request, import_enabled
from app.db.models import BotUser
from app.services.formatting import format_message, service_card
from app.services.service_import import (
    NOT_ENABLED,
    PENDING_NOTICE,
    RATE_LIMITED,
    ImportOutcome,
    ServiceImportError,
    import_existing_service,
)
from app.services.users import get_all_settings
from app.services import bot_feedback_texts as feedback

logger = logging.getLogger(__name__)
router = Router(name="service_import")

CANCEL_CODE = "svc_imp"
_SERVICES_BACK = InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, "svc:list")]])
PROMPT = format_message(
    "🔗 اتصال اشتراک قبلی",
    "لینک اشتراک سرویس را بفرستید.\n"
    "فقط سرویس‌هایی که زیر همین فروشگاه ساخته شده‌اند و به حساب دیگری وصل نیستند اضافه می‌شوند. "
    "پیام شما پس از دریافت پاک می‌شود.",
)
ADDED_TITLE = {
    ImportOutcome.ADDED: "✅ سرویس اضافه شد",
    ImportOutcome.UPGRADED: "✅ سرویس اضافه شد",
    ImportOutcome.ALREADY_YOURS: "ℹ️ این سرویس از قبل در لیست شماست",
}


class ServiceImportStates(StatesGroup):
    await_link = State()


async def _reopen_services_list(message, session, db_user, state, **kw) -> None:
    # Local import: reply_nav is the module that dispatches here.
    from app.bot.handlers.reply_nav import open_services_list

    await open_services_list(
        message, session, db_user, state, push=False,
        is_reseller_bot=bool(kw.get("is_reseller_bot")), reseller_owner_id=kw.get("reseller_owner_id"),
    )


register_cancel_code(CANCEL_CODE, CancelEntry(reopen=_reopen_services_list))


@router.callback_query(F.data == ADD_CALLBACK)
async def svc_add_existing(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    ui = await get_all_settings(session)
    if not import_enabled(ui):
        await callback.answer(NOT_ENABLED, show_alert=True)
        return
    await callback.answer()
    await ask_text(
        callback, state, prompt=PROMPT, cancel_code=CANCEL_CODE,
        fsm_state=ServiceImportStates.await_link, edit=True,
    )


@router.callback_query(F.data == HOME_ADD_CALLBACK)
async def svc_add_existing_from_home(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    """The home offer: the link prompt is a new message and the offer under the welcome goes away."""
    ui = await get_all_settings(session)
    if not import_enabled(ui):
        await callback.answer(NOT_ENABLED, show_alert=True)
        return
    await callback.answer()
    offer = callback.message
    await ask_text(callback, state, prompt=PROMPT, cancel_code=CANCEL_CODE, fsm_state=ServiceImportStates.await_link)
    if offer is not None:
        try:
            await offer.delete()
        except TelegramAPIError:
            logger.debug("home offer already gone", exc_info=True)


@router.message(ServiceImportStates.await_link)
async def svc_import_link(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text) or kb.is_home_text(message.text, ui):
        if not await try_legacy_cancel(
            message, state, session, db_user,
            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
        ):
            await state.set_state(None)
            await message.answer(feedback.CANCELLED)
        return
    raw_link = (message.text or "").strip()
    if not raw_link:
        await message.answer("لینک اشتراک را بفرستید یا انصراف بزنید.")
        return
    await delete_user_message(message)  # the link is a bearer secret
    if not import_enabled(ui):
        await state.set_state(None)
        await message.answer(NOT_ENABLED)
        return
    shop_id = int(reseller_owner_id) if is_reseller_bot and reseller_owner_id else None
    try:
        result = await import_existing_service(session, user=db_user, raw_link=raw_link, shop_id=shop_id)
    except ServiceImportError as exc:
        text = format_message("⚠️ افزودن سرویس", str(exc))
        if str(exc) == RATE_LIMITED:
            await state.set_state(None)
            await finish_text_step(message, state, text=text, inline=_SERVICES_BACK)
            return
        await message.answer(text, reply_markup=cancel_keyboard(CANCEL_CODE))
        return
    await state.set_state(None)
    if result.outcome == ImportOutcome.PENDING_APPROVAL:
        await finish_text_step(
            message, state, text=format_message("⏳ در انتظار تأیید", PENDING_NOTICE), inline=_SERVICES_BACK
        )
        await announce_new_request(message, session, db_user, result)
        return
    from app.bot.nav_inline import service_card_keyboard

    await finish_text_step(
        message, state,
        text=format_message(ADDED_TITLE[result.outcome], service_card(result.info)),
        inline=service_card_keyboard(int(result.service.id), ui),
    )
