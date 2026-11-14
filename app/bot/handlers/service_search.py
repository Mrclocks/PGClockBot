"""«🔍 جستجوی سرویس» on a long service list: the customer types part of a name."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import (
    CancelEntry,
    ask_text,
    finish_text_step,
    register_cancel_code,
    try_legacy_cancel,
)
from app.bot.nav_inline import SERVICES_LIST_CALLBACK, SERVICES_PAGE_SIZE, SERVICES_SEARCH_CALLBACK
from app.db.models import BotUser
from app.services.user_errors import domain_error_text
from app.services.service_scope import search_customer_services
from app.services.users import current_shop_reseller_id

router = Router(name="service_search")

CANCEL_CODE = "svc_find"
PROMPT = "🔍 بخشی از نام سرویس را بفرستید:"
NO_MATCH = "سرویسی با «{text}» پیدا نشد."
FOUND = "🔍 نتیجهٔ جستجوی «{text}»:"
SEARCH_AGAIN = "🔍 جستجوی دوباره"


class ServiceSearchStates(StatesGroup):
    query = State()


def _results_markup(services: list) -> InlineKeyboardMarkup:
    rows = list(kb.services_keyboard(services).inline_keyboard) if services else []
    rows.append([InlineKeyboardButton(text=SEARCH_AGAIN, callback_data=SERVICES_SEARCH_CALLBACK)])
    rows.append([kb.back_button(None, SERVICES_LIST_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "svc:find")
async def ask_service_search(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await ask_text(
        callback,
        state,
        prompt=PROMPT,
        cancel_code=CANCEL_CODE,
        return_to=SERVICES_LIST_CALLBACK,
        fsm_state=ServiceSearchStates.query,
        edit=True,
    )


@router.message(ServiceSearchStates.query)
async def show_service_search(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text) and await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    text = (message.text or "").strip()
    try:
        found = await search_customer_services(
            session, int(db_user.id), current_shop_reseller_id(), text, limit=SERVICES_PAGE_SIZE
        )
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    await state.set_state(None)
    shown = html.escape(text)
    await finish_text_step(
        message,
        state,
        text=(FOUND if found else NO_MATCH).format(text=shown),
        inline=_results_markup(found),
    )


async def _reopen_services(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **kw) -> None:
    # reply_nav imports the handler modules; importing it here at load time would be circular.
    from app.bot.handlers.reply_nav import open_services_list

    await open_services_list(
        message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=kw.get("is_reseller_bot", False),
        reseller_owner_id=kw.get("reseller_owner_id"),
    )


register_cancel_code(CANCEL_CODE, CancelEntry(reopen=_reopen_services))
