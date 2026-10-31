"""Small Telegram helpers — avoid noisy errors after a successful reply."""
from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession
from app.bot.chat_cleanup import replace_home
from app.services import bot_feedback_texts as feedback

logger = logging.getLogger("pgclock.bot")

from app.services.numbers import normalize_number_text, parse_float, parse_int


def normalize_bot_number_text(text: str | None) -> str:
    """Normalize Persian/Arabic digits and separators for int/float parsing."""
    return normalize_number_text(text)


def parse_bot_int(text: str | None, *, default: int | None = None) -> int:
    return parse_int(text, default=default)


def parse_bot_float(text: str | None, *, default: float | None = None) -> float:
    return parse_float(text, default=default)


def _is_not_modified(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "message is not modified" in text or "message to edit not found" in text


async def safe_edit_text(
    message: Message | None,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> bool:
    """edit_text that ignores benign Telegram errors. Returns True if edited."""
    if message is None:
        return False
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
        return True
    except TelegramBadRequest as e:
        if _is_not_modified(e):
            return False
        logger.warning("edit_text failed: %s", e)
        try:
            await message.answer(text, reply_markup=reply_markup, **kwargs)
            return True
        except Exception:
            logger.exception("fallback answer after edit_text failed")
            return False
    except Exception:
        logger.exception("edit_text unexpected error")
        try:
            await message.answer(text, reply_markup=reply_markup, **kwargs)
            return True
        except Exception:
            return False


async def attach_reply_keyboard(
    message: Message,
    reply_markup: ReplyKeyboardMarkup,
    *,
    text: str = "⌨️",
) -> Message | None:
    """Attach a reply keyboard on a *lasting* message.

    Never delete this message. On many Telegram clients (especially iOS),
    deleting the message that set ``ReplyKeyboardMarkup`` drops the custom
    keyboard **and** the input-field 4-square menu icon, leaving the system
    QWERTY keyboard open. Only remove/replace reply keyboards when the user
    must type (``cancel_reply`` / ``ForceReply``) or when a new lasting
    ``ReplyKeyboardMarkup`` is sent.
    """
    try:
        return await message.answer(text or "⌨️", reply_markup=reply_markup)
    except Exception:
        logger.warning("Could not attach reply keyboard", exc_info=True)
        return None


async def send_reply_keyboard_last(
    message: Message,
    text: str,
    reply_markup: ReplyKeyboardMarkup,
    *,
    ahead: list[tuple[str, Any]] | None = None,
    **send_kw: Any,
) -> Message | None:
    """Send optional inline-only messages first, then text **with** reply KB last.

    Telegram allows only one ``reply_markup`` per message. Any inline bubble
    (e.g. Mini App) sent *after* the reply-keyboard carrier leaves that carrier
    non-final; on iOS the custom keyboard and 4-square menu icon then hide and
    the system keyboard opens. Always finish a navigation turn with a lasting
    ``ReplyKeyboardMarkup`` unless the user must type free text.
    """
    for ahead_text, ahead_markup in ahead or []:
        try:
            await message.answer(ahead_text, reply_markup=ahead_markup)
        except Exception:
            logger.warning(
                "send_reply_keyboard_last: ahead message failed (%s)",
                ahead_text[:20],
                exc_info=True,
            )
    try:
        return await message.answer(text, reply_markup=reply_markup, **send_kw)
    except Exception:
        logger.warning("send_reply_keyboard_last: reply carrier failed", exc_info=True)
        return None


async def present_inline_with_reply_chrome(
    message: Message,
    *,
    text: str,
    inline: InlineKeyboardMarkup,
    reply: ReplyKeyboardMarkup,
    chrome_text: str,
    **send_kw: Any,
) -> None:
    """Dual-keyboard contract for choice screens.

    1. Content bubble carries the **inline** keyboard (Telegram forbids Reply+Inline
       on the same message; never convert Reply→Inline via ``edit_reply_markup``).
    2. A **lasting** follow-up carries the reply chrome (back/home or submenu).
       Never delete that follow-up — deletion clears the custom keyboard and the
       4-square menu icon on iOS.
    3. The reply chrome must match the screen (shop chrome in shop, apply chrome
       in reseller-apply, …) — never force the main menu unless the user is home.
    """
    await message.answer(text, reply_markup=inline, **send_kw)
    await attach_reply_keyboard(message, reply, text=chrome_text)


async def seed_reply_keyboard(
    message: Message,
    reply_markup,
    *,
    tip: str = "·",
    ephemeral: bool = False,
) -> None:
    """Register a reply keyboard with Telegram.

    ``ephemeral=True`` deletes the tip (legacy polish). Safe **only** when
    another lasting message in the same chat already carries the same
    ``ReplyKeyboardMarkup`` (e.g. welcome sent after this tip). Do **not** use
    ephemeral seeding as the *only* restore after ``cancel_reply()``, shop
    chrome, or home — tip-delete hides the custom keyboard and menu icon.
    Prefer ``attach_reply_keyboard`` / ``send_reply_keyboard_last``.
    """
    try:
        tip_msg = await message.answer(tip or "·", reply_markup=reply_markup)
    except Exception:
        logger.warning("Could not seed reply keyboard", exc_info=True)
        return
    if not ephemeral:
        return
    try:
        await tip_msg.delete()
    except Exception:
        logger.debug("seed_reply_keyboard: ignored failure", exc_info=True)


async def seed_persistent_reply_kb(message: Message) -> None:
    """Seed home reply keyboard on a lasting chrome message (safe after FSM)."""
    from app.bot import keyboards as kb

    await replace_home(
        await attach_reply_keyboard(message, kb.persistent_reply_keyboard(), text="🏠 منوی اصلی")
    )


async def finish_text_input(
    message: Message,
    reply_markup: ReplyKeyboardMarkup,
    *,
    note: str,
) -> None:
    """End an FSM prompt that used ``cancel_reply`` — restore lasting reply KB."""
    await message.answer(note, reply_markup=reply_markup)


async def clear_fsm_with_reply(
    message: Message,
    state,
    *,
    note: str = feedback.CANCELLED,
    session: AsyncSession | None = None,
    db_user=None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Clear FSM and restore the full main reply keyboard when context is available."""
    from app.bot import keyboards as kb
    from app.bot.menu_nav import restore_main_reply

    if session is not None and db_user is not None:
        await restore_main_reply(
            message,
            session,
            db_user,
            text=note,
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await state.clear()
    await message.answer(note, reply_markup=kb.persistent_reply_keyboard())
