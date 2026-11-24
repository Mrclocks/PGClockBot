"""Customer connection status reads snapshots and never starts network probes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.connection_status_public_ui import MonitorPublicLabel, public_cursor, public_status_keyboard, public_status_text
from app.bot.connection_status_ui import monitor_text
from app.bot.tg_utils import safe_edit_text
from app.bot.verbatim_input import NotVerbatimTextInput
from app.services.connection_status import PublicConnectionPage, public_connection_page
from app.services.db_safe import rollback_quiet
from app.services.users import DEFAULT_SETTINGS, get_all_settings


logger = logging.getLogger(__name__)
router = Router(name="connection_status")


async def _show(message: Message, session: AsyncSession, *, after_id: int, edit: bool) -> None:
    ui = dict(DEFAULT_SETTINGS)
    try:
        ui = await get_all_settings(session)
        now = datetime.now(timezone.utc)
        page = await public_connection_page(session, now=now, after_id=after_id)
        text, markup = public_status_text(page, ui, now=now), public_status_keyboard(page)
    except Exception as exc:
        await rollback_quiet(session)
        logger.warning("Public connection status unavailable error_type=%s", type(exc).__name__)
        text, markup = monitor_text(ui, "connection_status_error"), public_status_keyboard(PublicConnectionPage(()))
    if edit:
        await safe_edit_text(message, text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("status"))
@router.message(MonitorPublicLabel(), NotVerbatimTextInput())
async def connection_status(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(None)
    await _show(message, session, after_id=0, edit=False)


@router.callback_query(F.data.startswith("csp:"))
async def refresh_connection_status(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    try:
        after_id = public_cursor(callback.data or "")
    except ValueError:
        await rollback_quiet(session)
        await callback.message.answer("درخواست نامعتبر است؛ فهرست وضعیت را دوباره باز کنید.")
        return
    await _show(callback.message, session, after_id=after_id, edit=True)
