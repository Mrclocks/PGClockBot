"""Typed-input prompts without swapping the main ReplyKeyboard.

ask_text / finish_text_step keep the stable level-0 keyboard in place and use
an inline «انصراف» button (``nv:cancel:{code}``) instead of ``cancel_reply``.
Legacy reply-label «انصراف» remains handled by existing ``is_cancel_text`` paths.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser

logger = logging.getLogger(__name__)

router = Router(name="nav_input")

PROMPT_MSG_ID = "_nav_input_prompt_msg_id"
PROMPT_CHAT_ID = "_nav_input_prompt_chat_id"
CANCEL_CODE_KEY = "_nav_input_cancel_code"

CancelReopen = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class CancelEntry:
    """Fixed reopen target for ``nv:cancel:{code}`` — never trust raw callback data."""

    reopen: CancelReopen
    # Optional extra kwargs passed to reopen (besides message/session/db_user/state).
    reopen_kw: dict[str, Any] | None = None


# Populated by register_cancel_codes() at import of dependent handlers.
CANCEL_REGISTRY: dict[str, CancelEntry] = {}


def register_cancel_code(code: str, entry: CancelEntry) -> None:
    if not code or ":" in code or len(code) > 24:
        raise ValueError(f"invalid cancel code: {code!r}")
    CANCEL_REGISTRY[code] = entry


def cancel_keyboard(cancel_code: str, *, label: str = "انصراف") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"nv:cancel:{cancel_code}",
                )
            ]
        ]
    )


def _merge_inline(
    cancel_code: str,
    extra: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if extra is not None:
        rows.extend(list(extra.inline_keyboard))
    rows.extend(cancel_keyboard(cancel_code).inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_text(
    target: Message | CallbackQuery,
    state: FSMContext,
    *,
    prompt: str,
    cancel_code: str,
    fsm_state: State,
    extra_inline: InlineKeyboardMarkup | None = None,
    edit: bool = False,
    **send_kw: Any,
) -> Message | None:
    """Prompt for free text with an inline cancel button; main ReplyKeyboard untouched."""
    if cancel_code not in CANCEL_REGISTRY:
        # Staff codes live in staff_cancel; import lazily for direct handler tests.
        try:
            import app.bot.staff_cancel  # noqa: F401
        except Exception:
            pass
    if cancel_code not in CANCEL_REGISTRY:
        raise KeyError(f"cancel code not registered: {cancel_code!r}")
    await state.set_state(fsm_state)
    markup = _merge_inline(cancel_code, extra_inline)
    msg: Message | None = None
    if isinstance(target, CallbackQuery):
        if not target.message:
            return None
        base = target.message
        if edit:
            try:
                await base.edit_text(prompt, reply_markup=markup, **send_kw)
                msg = base
            except Exception:
                msg = await base.answer(prompt, reply_markup=markup, **send_kw)
        else:
            msg = await base.answer(prompt, reply_markup=markup, **send_kw)
    else:
        if edit:
            try:
                await target.edit_text(prompt, reply_markup=markup, **send_kw)
                msg = target
            except Exception:
                msg = await target.answer(prompt, reply_markup=markup, **send_kw)
        else:
            msg = await target.answer(prompt, reply_markup=markup, **send_kw)
    if msg is not None:
        await state.update_data(
            **{
                PROMPT_MSG_ID: int(msg.message_id),
                PROMPT_CHAT_ID: int(msg.chat.id),
                CANCEL_CODE_KEY: cancel_code,
            }
        )
    return msg


async def finish_text_step(
    message: Message,
    state: FSMContext,
    *,
    text: str,
    inline: InlineKeyboardMarkup | None = None,
    **send_kw: Any,
) -> Message | None:
    """Turn the stored prompt into the next panel (edit), or send one new message."""
    data = await state.get_data()
    prompt_id = data.get(PROMPT_MSG_ID)
    chat_id = data.get(PROMPT_CHAT_ID)
    await state.update_data(
        **{PROMPT_MSG_ID: None, PROMPT_CHAT_ID: None, CANCEL_CODE_KEY: None}
    )
    if prompt_id and chat_id and int(chat_id) == int(message.chat.id):
        try:
            await message.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(prompt_id),
                text=text,
                reply_markup=inline,
                **send_kw,
            )
            return None
        except Exception:
            logger.debug(
                "finish_text_step edit failed; falling back to answer",
                exc_info=True,
            )
    return await message.answer(text, reply_markup=inline, **send_kw)


async def remember_prompt(
    state: FSMContext | None,
    message: Message | None,
    *,
    cancel_code: str | None = None,
) -> None:
    """Store panel ids so a later photo/text reply can edit the same bubble."""
    if state is None or message is None:
        return
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    msg_id = getattr(message, "message_id", None)
    if chat_id is None or msg_id is None:
        return
    payload: dict[str, Any] = {
        PROMPT_MSG_ID: int(msg_id),
        PROMPT_CHAT_ID: int(chat_id),
    }
    if cancel_code is not None:
        payload[CANCEL_CODE_KEY] = cancel_code
    await state.update_data(**payload)


async def try_legacy_cancel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> bool:
    """Reopen the previous panel when FSM still knows a cancel_code.

    Used for stale ReplyKeyboard «انصراف» taps after ask_text migration.
    Returns True when handled.
    """
    data = await state.get_data()
    code = data.get(CANCEL_CODE_KEY)
    entry = CANCEL_REGISTRY.get(str(code)) if code else None
    if entry is None:
        return False
    try:
        await state.set_state(None)
    except Exception:
        pass
    await state.update_data(
        **{PROMPT_MSG_ID: None, PROMPT_CHAT_ID: None, CANCEL_CODE_KEY: None}
    )
    kw = dict(entry.reopen_kw or {})
    kw.setdefault("is_reseller_bot", is_reseller_bot)
    kw.setdefault("reseller_owner_id", reseller_owner_id)
    try:
        await entry.reopen(message, session, db_user, state, **kw)
    except TypeError:
        await entry.reopen(message, session, db_user, state)
    return True


def with_cancel_row(
    markup: InlineKeyboardMarkup | None,
    cancel_code: str,
) -> InlineKeyboardMarkup:
    """Append an inline «انصراف» row to an existing markup (or build one)."""
    return _merge_inline(cancel_code, markup)


@router.callback_query(F.data.startswith("nv:cancel:"))
async def nv_cancel_input(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    raw = callback.data or ""
    code = raw.split(":", 2)[-1] if raw.startswith("nv:cancel:") else ""
    entry = CANCEL_REGISTRY.get(code)
    if entry is None:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await callback.answer()
    # Clear typed-input FSM; preserve nav stack keys via clear of state only.
    try:
        await state.set_state(None)
    except Exception:
        pass
    data = await state.get_data()
    await state.update_data(
        **{PROMPT_MSG_ID: None, PROMPT_CHAT_ID: None, CANCEL_CODE_KEY: None}
    )
    if not callback.message:
        return
    kw = dict(entry.reopen_kw or {})
    kw.setdefault("is_reseller_bot", is_reseller_bot)
    kw.setdefault("reseller_owner_id", reseller_owner_id)
    try:
        await entry.reopen(
            callback.message,
            session,
            db_user,
            state,
            **kw,
        )
    except TypeError:
        # Reopen helpers that do not take bot-context kwargs.
        await entry.reopen(callback.message, session, db_user, state)
    _ = data
