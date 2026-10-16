"""Thin Owner-only connection monitoring entry and inline action handlers."""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.connection_status_admin_ui import (
    MonitorCommand, monitor_error, parse_monitor_callback, show_monitor_home, show_monitor_target,
)
from app.bot.connection_status_ui import MONITOR_HOME_CALLBACK, MonitorAdminLabel, bot_monitor_access, button, config_picker_text, monitor_text
from app.bot.keyboards import back_button
from app.bot.handlers.connection_status_admin_input import CANCEL_CODE, MonitorInput
from app.bot.nav_input import ask_text
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.connection_monitor_access import MonitorAccess
from app.services.connection_monitor_actions import edit_monitor_target, select_monitor_config
from app.services.connection_monitor_admin import monitor_config_choices, set_monitor_enabled
from app.services.connection_monitor_preferences import pause_monitor_target
from app.services.connection_monitor_queue import queue_manual_check
from app.services.connection_monitor_views import monitor_target_view
from app.services.users import get_all_settings


router = Router(name="connection_status_admin")


@router.message(Command("connection_admin"))
@router.message(MonitorAdminLabel())
async def monitor_admin_entry(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext,
                                is_reseller_bot: bool = False) -> None:
    try:
        access = await bot_monitor_access(session, db_user, is_reseller_bot=is_reseller_bot)
        await state.set_state(None)
        await show_monitor_home(message, session, access, edit=False)
    except Exception as exc:
        await monitor_error(message, session, exc)


async def _prompt(callback: CallbackQuery, session: AsyncSession, access: MonitorAccess,
                   state: FSMContext, command: MonitorCommand) -> None:
    if command.action != "account":
        await monitor_target_view(session, access, target_id=command.identifier, now=datetime.now(timezone.utc))
        await state.update_data(monitor_target_id=command.identifier)
    prompts = {"account": (MonitorInput.account, "connection_status_account_prompt"),
               "name": (MonitorInput.public_name, "connection_status_name_prompt"),
               "order": (MonitorInput.display_order, "connection_status_order_prompt")}
    fsm, key = prompts[command.action]
    ui = await get_all_settings(session)
    await session.commit()
    text = monitor_text(ui, key)
    if command.action == "account":
        text += "\nثبت یا تغییر کاربر تست، تست‌ها را متوقف و کانفیگ‌ها را از فهرست کاربران مخفی می‌کند."
    await ask_text(callback, state, prompt=text, cancel_code=CANCEL_CODE, fsm_state=fsm, edit=True)


async def _config_picker(message: Message, session: AsyncSession, access: MonitorAccess, *, after_id: int) -> None:
    page = await monitor_config_choices(session, access, offset=after_id)
    rows = [[button(("⚪ " if choice.issue else "") + choice.name, f"cma:pick:{choice.config_key}")] for choice in page.choices]
    if page.next_offset is not None:
        rows.append([button("صفحهٔ بعد", f"cma:hosts:{page.next_offset}")])
    ui = await get_all_settings(session)
    await session.commit()
    rows.append([back_button(ui, MONITOR_HOME_CALLBACK)])
    await safe_edit_text(message, config_picker_text(page), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _mutate(session: AsyncSession, access: MonitorAccess, command: MonitorCommand) -> int | None:
    if command.action == "pick":
        return await select_monitor_config(session, access, config_key=command.config_key)
    if command.action == "public":
        return await edit_monitor_target(session, access, target_id=command.identifier, field="is_public", value=command.flag)
    if command.action == "stop":
        await pause_monitor_target(session, access, target_id=command.identifier)
    elif command.action == "check":
        await queue_manual_check(session, access, target_id=command.identifier, now=datetime.now(timezone.utc))
    elif command.action in ("run", "halt"):
        await set_monitor_enabled(session, access, enabled=command.action == "run")
        return None
    return command.identifier


@router.callback_query(F.data.startswith("cma:"))
async def monitor_admin_callback(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext,
                                  is_reseller_bot: bool = False) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    try:
        command = parse_monitor_callback(callback.data or "")
        access = await bot_monitor_access(session, db_user, is_reseller_bot=is_reseller_bot)
        if command.action in ("account", "name", "order"):
            await _prompt(callback, session, access, state, command)
            return
        await state.set_state(None)
        if command.action == "home":
            await show_monitor_home(callback.message, session, access, after_id=command.identifier)
        elif command.action == "hosts":
            await _config_picker(callback.message, session, access, after_id=command.identifier)
        elif command.action == "pause" or (command.action == "run" and command.identifier == 0):
            await session.commit()
            data = f"cma:stop:{command.identifier}" if command.action == "pause" else "cma:halt:0"
            markup = InlineKeyboardMarkup(inline_keyboard=[[button("تأیید توقف تست‌ها و نمایش عمومی", data)], [button("انصراف", "cma:home:0")]])
            await safe_edit_text(callback.message, "تست‌ها متوقف شوند؟ کانفیگ‌ها از فهرست کاربران هم مخفی می‌شوند.", reply_markup=markup)
        elif command.action == "item":
            await show_monitor_target(callback.message, session, access, target_id=command.identifier)
        else:
            target_id = await _mutate(session, access, command)
            if target_id is None:
                await show_monitor_home(callback.message, session, access)
            else:
                await show_monitor_target(callback.message, session, access, target_id=target_id)
    except Exception as exc:
        await monitor_error(callback.message, session, exc, edit=True)
