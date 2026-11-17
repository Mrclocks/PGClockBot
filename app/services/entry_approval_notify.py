"""Telegram messages of «تأیید ورود کاربران جدید»: the admins' request card and the customer's answer.

The request card goes to the shop the customer is entering (its owner and bot admins, through
its own bot) or to the platform admins, with approve, reject and block buttons. Its copies are
remembered under one replace key, so once any admin decides, every copy is edited to show the
decision. Sending is best effort: the request is stored and listed in the web panel anyway.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser
from app.services.entry_approval import (
    STATUS_APPROVED,
    STATUS_BLOCKED,
    Action,
    Decision,
)
from app.services.entry_approval_config import (
    KEY_APPROVED_TEXT,
    KEY_REJECTED_TEXT,
    NOTIFY_KEY,
    read_config,
)
from app.services.formatting import format_message, format_user_label, info_block, kv_line
from app.services.notifications import _dispatch_dual_notify
from app.services.payer_notify import notify_payer
from app.services.referral_scope import is_referral_of_shop
from app.services.replaceable_notices import settle_notices
from app.services.support_button import support_markup
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

ADMIN_CALLBACK_PREFIX = "eap:"
CUSTOMER_ENTER_CALLBACK = "eapx:go"
CUSTOMER_RETRY_CALLBACK = "eapx:retry"
ACTION_CODES: dict[str, Action] = {"ok": Action.APPROVE, "no": Action.REJECT, "bk": Action.BLOCK}
REVIEW_PATH = "/settings?tab=limits"
REVIEW_BUTTON = "🗂 فهرست درخواست‌ها"
ENTER_BUTTON = "🚀 ورود به ربات"
RETRY_BUTTON = "🔁 درخواست دوباره"
CUSTOMER_TITLE = "ورود به ربات"
REQUEST_TITLE = "🆕 درخواست ورود کاربر جدید"
DECISION_LABELS: dict[str, str] = {
    STATUS_APPROVED: "✅ تأیید شد",
    "rejected": "❌ رد شد",
    STATUS_BLOCKED: "⛔ مسدود شد",
}


def admin_callback(action_code: str, approval_id: int) -> str:
    return f"{ADMIN_CALLBACK_PREFIX}{action_code}:{int(approval_id)}"


def parse_admin_callback(data: str | None) -> tuple[Action, int] | None:
    """``(action, approval id)`` of an admin button, or None when the data is not one of ours."""
    parts = (data or "").split(":")
    if len(parts) != 3 or f"{parts[0]}:" != ADMIN_CALLBACK_PREFIX or parts[1] not in ACTION_CODES:
        return None
    if not parts[2].isdigit() or len(parts[2]) > 12:
        return None
    return ACTION_CODES[parts[1]], int(parts[2])


def replace_key(approval_id: int) -> str:
    return f"entry:{int(approval_id)}"


def _admin_markup(approval_id: int, *, shop: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ تأیید", callback_data=admin_callback("ok", approval_id)),
            InlineKeyboardButton(text="❌ رد", callback_data=admin_callback("no", approval_id)),
        ],
        [InlineKeyboardButton(text="⛔ مسدود", callback_data=admin_callback("bk", approval_id))],
    ]
    base = get_settings().public_base_url.rstrip("/")
    if base.startswith("https://"):
        path = REVIEW_PATH.replace("/settings", "/shop-settings", 1) if shop else REVIEW_PATH
        rows.append([InlineKeyboardButton(text=REVIEW_BUTTON, url=base + path)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _customer_lines(user: BotUser) -> list[str]:
    name = (user.full_name or "").strip()
    return [
        kv_line("👤", "کاربر", format_user_label(user)),
        kv_line("📝", "نام", html.escape(name)) if name and user.username else "",
        kv_line("🆔", "آیدی عددی", f"<code>{int(user.telegram_id)}</code>"),
    ]


def request_text(user: BotUser, *, shop_key: int, phone: str | None = None) -> str:
    lines = _customer_lines(user)
    if phone:
        lines.append(kv_line("📱", "شماره تماس", f"<code>{html.escape(phone[:32])}</code>"))
    if is_referral_of_shop(user, shop_key or None):
        lines.append(kv_line("🤝", "معرف", "دارد"))
    return format_message(REQUEST_TITLE, info_block(lines))


def decided_text(user: BotUser | None, status: str, *, actor: str) -> str:
    lines = _customer_lines(user) if user is not None else []
    lines.append(kv_line("📌", "نتیجه", DECISION_LABELS.get(status, status)))
    if actor:
        lines.append(kv_line("🧑‍💼", "توسط", html.escape(actor)))
    return format_message(REQUEST_TITLE, info_block(lines))


async def notify_admins(
    bot: Bot, session: AsyncSession, *, user: BotUser, approval_id: int, shop_key: int, phone: str | None = None
) -> int:
    """Send the request card; returns how many chats got it (0 when nobody could be reached)."""
    try:
        return await _dispatch_dual_notify(
            bot, session, NOTIFY_KEY, request_text(user, shop_key=shop_key, phone=phone),
            markup=_admin_markup(approval_id, shop=bool(shop_key)),
            ticket_user_id=int(user.id), ticket_reseller_id=shop_key or None,
            replace_key=replace_key(approval_id),
        )
    except (TelegramAPIError, OSError, TimeoutError):
        logger.warning("entry approval notice failed approval=%s", approval_id, exc_info=True)
        return 0


async def settle_admin_cards(session: AsyncSession, bot: Bot, decision: Decision, *, actor: str) -> None:
    """Show the decision on every admin's copy of the request card."""
    user = await session.get(BotUser, decision.user_id)
    await settle_notices(session, bot, replace_key(decision.approval_id), decided_text(user, decision.status, actor=actor))


def _customer_markup(status: str, *, show_support: bool) -> InlineKeyboardMarkup | None:
    if status == STATUS_APPROVED:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ENTER_BUTTON, callback_data=CUSTOMER_ENTER_CALLBACK)]])
    return support_markup() if show_support else None


async def tell_customer(session: AsyncSession, decision: Decision) -> bool:
    """Send the customer the shop's approval or rejection text, through that shop's bot."""
    ui = await get_all_settings(session, reseller_id=decision.shop_key or None)
    key = KEY_APPROVED_TEXT if decision.status == STATUS_APPROVED else KEY_REJECTED_TEXT
    return await notify_payer(
        session, decision.user_id, title=CUSTOMER_TITLE, body=ui.get(key) or "",
        shop_id=decision.shop_key or None,
        reply_markup=_customer_markup(decision.status, show_support=read_config(ui).show_support),
    )
