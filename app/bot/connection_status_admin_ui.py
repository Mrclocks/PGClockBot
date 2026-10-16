"""Shared admin rendering, callback parsing and safe monitoring errors."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.connection_status_ui import MONITOR_HOME_CALLBACK, admin_page_keyboard, admin_page_text, bot_monitor_access, target_keyboard, target_text
from app.bot.keyboards import back_button
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.connection_monitor_access import MonitorAccess
from app.services.connection_monitor_views import monitor_admin_page, monitor_target_view
from app.services.connection_monitor_errors import monitor_error_message
from app.services.connection_monitor_subscription import validate_config_key
from app.services.db_safe import rollback_quiet
from app.services.users import get_all_settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorCommand:
    action: str
    identifier: int = 0
    flag: bool = False
    config_key: str = ""


def parse_monitor_callback(raw: str) -> MonitorCommand:
    parts = raw.split(":")
    arity = {"account": 2, "home": 3, "hosts": 3, "item": 3, "pick": 3,
             "check": 3, "pause": 3, "stop": 3, "name": 3, "order": 3,
             "run": 3, "halt": 3, "public": 4}
    if len(parts) < 2 or parts[0] != "cma" or arity.get(parts[1]) != len(parts):
        raise ValueError("درخواست نامعتبر است.")
    if len(parts) == 2:
        return MonitorCommand(parts[1])
    if parts[1] == "pick":
        return MonitorCommand("pick", config_key=validate_config_key(parts[2]))
    if not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 10:
        raise ValueError("شناسه نامعتبر است.")
    identifier = int(parts[2])
    minimum = 0 if parts[1] in ("home", "hosts", "run", "halt") else 1
    if not minimum <= identifier <= 2147483647:
        raise ValueError("شناسه نامعتبر است.")
    if parts[1] in ("run", "halt") and identifier not in (0, 1):
        raise ValueError("درخواست نامعتبر است.")
    if len(parts) == 4 and parts[3] not in ("0", "1"):
        raise ValueError("درخواست نامعتبر است.")
    return MonitorCommand(parts[1], identifier, len(parts) == 4 and parts[3] == "1")


async def show_monitor_home(message: Message, session: AsyncSession, access: MonitorAccess, *,
                             after_id: int = 0, edit: bool = True) -> None:
    page = await monitor_admin_page(session, access, now=datetime.now(timezone.utc), after_id=after_id)
    ui = await get_all_settings(session)
    await session.commit()
    text, markup = admin_page_text(page, ui), admin_page_keyboard(page, ui)
    if edit:
        await safe_edit_text(message, text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def show_monitor_target(message: Message, session: AsyncSession, access: MonitorAccess, *, target_id: int) -> None:
    target = await monitor_target_view(session, access, target_id=target_id, now=datetime.now(timezone.utc))
    ui = await get_all_settings(session)
    await session.commit()
    await safe_edit_text(message, target_text(target), reply_markup=target_keyboard(target, ui))


async def reopen_monitor_home(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext,
                               **kwargs: object) -> None:
    access = await bot_monitor_access(session, db_user, is_reseller_bot=bool(kwargs.get("is_reseller_bot")))
    await state.set_state(None)
    # Cancel from the inline prompt edits that prompt back into the menu; a typed «انصراف» is the
    # user's own message, which cannot be edited, so the menu is sent fresh.
    from_bot = bool(message.from_user and message.from_user.is_bot)
    await show_monitor_home(message, session, access, edit=from_bot)


async def monitor_error(message: Message, session: AsyncSession, error: Exception, *, edit: bool = False) -> None:
    """Show a safe reason. From an inline button the menu message itself becomes the error with
    a back button; a typed reply has no bot message to edit, so the reason is sent as a message."""
    await rollback_quiet(session)
    # Domain messages are fixed Persian strings; never echo panel/DB/core errors.
    text = monitor_error_message(error)
    logger.warning("Monitoring admin action failed error_type=%s reason=%s", type(error).__name__,
                   getattr(error, "code", "action_failed"))
    if edit:
        markup = InlineKeyboardMarkup(inline_keyboard=[[back_button(None, MONITOR_HOME_CALLBACK)]])
        await safe_edit_text(message, html.escape(text), reply_markup=markup)
        return
    await message.answer(html.escape(text))
