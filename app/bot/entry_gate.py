"""The bot side of «تأیید ورود کاربران جدید»: keep a customer outside until an admin lets them in.

``EntryApprovalMiddleware`` stops every update of a customer who is not approved in this bot.
``/start``, the join and terms buttons pass through (they run their own gates first) and end in
``render_home``, which calls :func:`run_entry_gate` before drawing any menu.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
    Update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.entry_approval import (
    GateState,
    GateStep,
    claim_admin_notice,
    gate_config,
    resolve_gate,
    shop_key_of,
)
from app.services.entry_approval_config import (
    KEY_PENDING_TEXT,
    KEY_PHONE_TEXT,
    KEY_REJECTED_TEXT,
    EntryApprovalConfig,
)
from app.services.entry_approval_notify import (
    ADMIN_CALLBACK_PREFIX,
    CUSTOMER_RETRY_CALLBACK,
    RETRY_BUTTON,
    notify_admins,
)
from app.services.reseller_access import effective_menu_role
from app.services.rich_text import outbound_setting_text
from app.services.support_button import support_row
from app.services.users import DEFAULT_SETTINGS, get_all_settings

logger = logging.getLogger(__name__)

PHONE_BUTTON = "📱 ارسال شماره تماس"
WAIT_TOAST = "درخواست ورود شما هنوز تأیید نشده است"
CLOSED_TOAST = "دسترسی شما به این ربات باز نیست"
# The «تأیید ورود کاربران» subsection of «ورود و عضویت», shared by the owner and shop settings menus.
ENTRY_APPROVAL_SUB = "approval"
ENTRY_APPROVAL_SUB_TITLE = "تأیید ورود کاربران"
ENTRY_APPROVAL_BOT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("entry_approval_enabled", "ورود فقط با تأیید ادمین", "toggle"),
    ("entry_approval_skip_referred", "معافیت ورود با لینک دعوت", "toggle"),
    ("entry_approval_require_phone", "دریافت شماره قبل از درخواست", "toggle"),
    ("entry_approval_support", "دکمهٔ پشتیبانی در انتظار", "toggle"),
    ("entry_approval_retry_hours", "درخواست دوباره بعد از رد (ساعت، ۰ = هرگز)", "number"),
    ("entry_approval_expire_days", "رد خودکار بی‌پاسخ (روز، ۰ = خاموش)", "number"),
    ("entry_approval_phone_text", "پیام دریافت شماره", "textarea"),
    ("entry_approval_pending_text", "پیام در انتظار تأیید", "textarea"),
    ("entry_approval_approved_text", "پیام تأیید", "textarea"),
    ("entry_approval_rejected_text", "پیام رد", "textarea"),
)
# Callbacks that must reach their handlers while the customer is still outside.
_PASS_PREFIXES = ("forcejoin:", "terms:", "eapx:", ADMIN_CALLBACK_PREFIX)


def _setting_text(ui: dict[str, str], key: str) -> str:
    return ui.get(key) or DEFAULT_SETTINGS[key]


def _retry_line(retry_at: datetime) -> str:
    hours = max(1, math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds() / 3600))
    return f"\n\nبعد از {hours} ساعت می‌توانید دوباره درخواست بدهید."


def _gate_message(state: GateState, ui: dict[str, str], config: EntryApprovalConfig) -> tuple[str, dict[str, Any], InlineKeyboardMarkup | None]:
    rows: list[list[InlineKeyboardButton]] = []
    append = ""
    if state.step is GateStep.PENDING:
        key = KEY_PENDING_TEXT
    else:
        key = KEY_REJECTED_TEXT
        if state.step is GateStep.REJECTED and state.retry_at is not None:
            if state.retry_at <= datetime.now(timezone.utc):
                rows.append([InlineKeyboardButton(text=RETRY_BUTTON, callback_data=CUSTOMER_RETRY_CALLBACK)])
            else:
                append = _retry_line(state.retry_at)
    if config.show_support:
        rows.append(support_row())
    text, kwargs = outbound_setting_text(_setting_text(ui, key), append=append or None)
    return text, kwargs, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def show_gate(message: Message, state: GateState, ui: dict[str, str], config: EntryApprovalConfig) -> None:
    """Tell the customer where their request stands (or ask for the phone first)."""
    if state.step is GateStep.NEEDS_PHONE:
        text, kwargs = outbound_setting_text(_setting_text(ui, KEY_PHONE_TEXT))
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=PHONE_BUTTON, request_contact=True)]], resize_keyboard=True, one_time_keyboard=True,
        )
        await message.answer(text, reply_markup=keyboard, **kwargs)
        return
    text, kwargs, markup = _gate_message(state, ui, config)
    await message.answer(text, reply_markup=markup or ReplyKeyboardRemove(), **kwargs)


async def run_entry_gate(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    ui: dict[str, str],
    shop_id: int | None,
    phone: str | None = None,
) -> bool:
    """True when the customer must stay outside; the right message has then been sent.

    The caller has already checked that this person uses the bot as a customer.
    """
    shop_key = shop_key_of(shop_id)
    config = await gate_config(session, shop_key, ui)
    if config is None:
        return False
    state = await resolve_gate(session, db_user, shop_key=shop_key, config=config)
    if state.is_open:
        return False
    await notify_if_due(message.bot, session, db_user, state, shop_key=shop_key, phone=phone)
    await show_gate(message, state, ui, config)
    return True


async def notify_if_due(
    bot: Bot | None, session: AsyncSession, db_user: BotUser, state: GateState, *, shop_key: int, phone: str | None = None
) -> None:
    """Tell the admins about a new request, once (the claim makes a repeat a no-op)."""
    if bot is None or not state.needs_notice or state.approval_id is None:
        return
    if await claim_admin_notice(session, state.approval_id):
        await notify_admins(bot, session, user=db_user, approval_id=state.approval_id, shop_key=shop_key, phone=phone)


def _event_parts(event: TelegramObject) -> tuple[Message | None, CallbackQuery | None]:
    inner = event
    if isinstance(event, Update):
        inner = event.message or event.callback_query
    if isinstance(inner, CallbackQuery):
        message = inner.message if isinstance(inner.message, Message) else None
        return message, inner
    return (inner if isinstance(inner, Message) else None), None


def _passes_through(message: Message | None, callback: CallbackQuery | None) -> bool:
    if callback is not None:
        return (callback.data or "").startswith(_PASS_PREFIXES)
    if message is None:
        return True
    text = (message.text or "").strip()
    # /start runs the join and terms gates first; render_home then runs this one.
    return text.startswith("/start") or message.contact is not None


class EntryApprovalMiddleware(BaseMiddleware):
    """Stop a customer who is not approved in this bot (after the join and terms gates)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession | None = data.get("session")
        db_user: BotUser | None = data.get("db_user")
        message, callback = _event_parts(event)
        if session is None or db_user is None or _passes_through(message, callback):
            return await handler(event, data)
        shop_id = data.get("reseller_owner_id") if data.get("is_reseller_bot") else None
        shop_key = shop_key_of(shop_id)
        ui = await get_all_settings(session)
        config = await gate_config(session, shop_key, ui)
        if config is None or await self._role(session, db_user, data) != "user":
            return await handler(event, data)
        state = await resolve_gate(session, db_user, shop_key=shop_key, config=config)
        if state.is_open:
            return await handler(event, data)
        await notify_if_due(data.get("bot"), session, db_user, state, shop_key=shop_key)
        await self._refuse(message, callback, state, ui, config)
        return None

    @staticmethod
    async def _role(session: AsyncSession, db_user: BotUser, data: dict[str, Any]) -> str:
        role = data.get("menu_role")
        if role is None:
            role = await effective_menu_role(
                session, db_user, is_reseller_bot=bool(data.get("is_reseller_bot")),
                reseller_owner_id=data.get("reseller_owner_id"),
            )
            data["menu_role"] = role
        return str(role)

    @staticmethod
    async def _refuse(
        message: Message | None, callback: CallbackQuery | None, state: GateState, ui: dict[str, str], config: EntryApprovalConfig
    ) -> None:
        try:
            if callback is not None:
                await callback.answer(WAIT_TOAST if state.step is GateStep.PENDING else CLOSED_TOAST, show_alert=True)
            elif message is not None:
                await show_gate(message, state, ui, config)
        except TelegramAPIError:
            logger.debug("entry gate notice failed", exc_info=True)
