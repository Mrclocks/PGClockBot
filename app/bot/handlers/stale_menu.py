"""Last-resort handler: plain text nobody else claimed, usually a stale keyboard button."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.chat_screen import fresh_keyboard
from app.bot.nav_chrome import heal_main_reply
from app.db.models import BotUser
from app.services.stale_menu import should_send_stale_notice

logger = logging.getLogger(__name__)

router = Router(name="stale_menu")

STALE_NOTICE_STATE_KEY = "_stale_notice_at"
STALE_NOTICE_TEXT = "منو به‌روز شده است؛ از دکمه‌های زیر ادامه دهید."


@router.message(
    F.text,
    F.chat.type == "private",
    ~F.text.startswith("/"),
    StateFilter(None),
)
async def stale_menu_notice(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    data = await state.get_data()
    if not should_send_stale_notice(data.get(STALE_NOTICE_STATE_KEY)):
        return
    await state.update_data(
        **{STALE_NOTICE_STATE_KEY: datetime.now(timezone.utc).timestamp()}
    )
    try:
        # The person is pressing a button of an old keyboard: send the current one again.
        with fresh_keyboard():
            await heal_main_reply(
                message,
                session,
                db_user,
                text=STALE_NOTICE_TEXT,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
    except TelegramAPIError:
        logger.warning("stale menu notice failed", exc_info=True)
