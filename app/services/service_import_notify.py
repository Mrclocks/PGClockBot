"""Tell the admins who can decide that a customer asked to add an existing service."""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser
from app.services.formatting import format_message, format_user_label, info_block, kv_line
from app.services.notifications import _dispatch_dual_notify

logger = logging.getLogger(__name__)

NOTIFY_KEY = "notify_service_import_request"
REVIEW_PATH = "/settings?tab=service_import"
REVIEW_BUTTON = "بررسی درخواست‌ها"


def _review_markup(*, shop: bool) -> InlineKeyboardMarkup | None:
    base = get_settings().public_base_url.rstrip("/")
    if not base.startswith("https://"):
        return None
    path = REVIEW_PATH.replace("/settings", "/shop-settings", 1) if shop else REVIEW_PATH
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=REVIEW_BUTTON, url=base + path)]])


async def notify_service_import_request(
    bot: Bot, session: AsyncSession, *, user: BotUser, panel_username: str, request_id: int, shop_id: int | None
) -> int:
    """Notify the shop the request was made in (or the platform admins); returns how many chats got it.

    Best effort: the request is already stored and shows in the settings tab, so a Telegram
    failure never fails the customer's flow.
    """
    text = format_message(
        "➕ درخواست افزودن سرویس قبلی",
        info_block([
            kv_line("🔢", "درخواست", f"#{int(request_id)}"),
            kv_line("👤", "مشتری", html.escape(format_user_label(user))),
            kv_line("🧾", "کاربر پنل", f"<code>{html.escape(panel_username or '—')}</code>"),
        ]) + "\n\nتأیید یا رد در تب «افزودن سرویس قبلی» تنظیمات انجام می‌شود.",
    )
    try:
        return await _dispatch_dual_notify(
            bot, session, NOTIFY_KEY, text,
            markup=_review_markup(shop=bool(shop_id)),
            ticket_user_id=int(user.id), ticket_reseller_id=int(shop_id) if shop_id else None,
        )
    except (TelegramAPIError, OSError, TimeoutError):
        logger.warning("service import request notice failed request=%s", request_id, exc_info=True)
        return 0
