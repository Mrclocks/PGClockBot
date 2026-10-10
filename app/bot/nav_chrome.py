"""Single staff-nav contract for Option B (inline-only panels).

Telegram API facts
------------------
- ``ReplyKeyboardMarkup`` always sends the button label as a new user message.
- ``InlineKeyboardMarkup`` uses callbacks; the bot can edit one live message.

Professional layout
-------------------
**ReplyKeyboard (stable, level-0 only)**
  - Platform admin → customer menu + one «پنل ادمین» entry
  - Reseller shop bot → «پنل نماینده» + «پیش‌نمایش»
  - Nested groups / leaves never live on the reply keyboard

**Inline panel (one tracked message, edit-in-place)**
  - Admin home → four groups; group → leaves; leaf hubs → actions
  - Back climbs the inline stack; never swaps an inline hub for a reply submenu

**cancel_reply (free-text FSM)**
  - On cancel / finish → restore the lasting main ReplyKeyboard
  - Optionally re-present the previous inline hub (still inline)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.users import get_all_settings

ReopenPanel = Callable[..., Awaitable[Any]]


async def lasting_staff_reply(
    session: AsyncSession,
    db_user: BotUser,
    *,
    classic: ReplyKeyboardMarkup | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    ui: dict | None = None,
) -> ReplyKeyboardMarkup:
    """Always return the stable main ReplyKeyboard (Option B).

    ``classic`` is ignored — kept for call-site compatibility while handlers
    finish dropping submenu chrome builders.
    """
    _ = classic
    from app.bot.menu_nav import build_main_reply_keyboard

    if ui is None:
        ui = await get_all_settings(session)
    main_kb, _, _ = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        ui=ui,
    )
    return main_kb


async def answer_staff_nav(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    text: str,
    classic: ReplyKeyboardMarkup | None = None,
    state: FSMContext | None = None,
    reopen_panel: ReopenPanel | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    ui: dict | None = None,
    clear_state: bool = False,
    **reopen_kw: Any,
) -> None:
    """Heal reply chrome after cancel/finish; optionally restore an inline hub."""
    if ui is None:
        ui = await get_all_settings(session)
    if clear_state and state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    markup = await lasting_staff_reply(
        session,
        db_user,
        classic=classic,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        ui=ui,
    )
    await message.answer(text, reply_markup=markup)
    if reopen_panel is not None:
        await reopen_panel(
            message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=is_reseller_bot,
            **reopen_kw,
        )
