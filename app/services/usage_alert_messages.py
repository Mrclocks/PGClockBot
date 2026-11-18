"""What the low-time and low-volume alerts say.

When an alert is due is decided in ``alert_thresholds``. Both texts can be
edited per shop in the alert settings; placeholders are filled through ``render_message_template``,
which escapes every value for Telegram HTML and leaves unknown placeholders as they are.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from app.services.daily_report import format_jalali_parts
from app.services.formatting import format_bytes_ratio
from app.services.message_variables import DOMAIN_USAGE_ALERT, render_message_template
from app.services.numbers import to_fa_digits

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400
TIME_TEXT_KEY = "user_alert_time_text"
TRAFFIC_TEXT_KEY = "user_alert_traffic_text"
DEFAULT_TIME_TEXT = (
    "⏰ سرویس <b>{service_name}</b> رو به پایان است: {days_left} روز دیگر، تا {expire_date}، اعتبار دارد."
)
DEFAULT_TRAFFIC_TEXT = "📉 حجم سرویس <b>{service_name}</b> رو به پایان است: {traffic_used} مصرف شده است."
RENEW_HINT = "برای تمدید، دکمهٔ زیر را بزنید."
NO_BUTTON_HINT = "از «سرویس‌های من» تمدید کنید."


def alerts_off_settings_page(ui: dict, *, is_owner: bool, can_edit_shop: bool) -> str | None:
    """Where staff can switch customer alerts on, while they are off; None when on or not editable.

    An empty ``ui`` means the settings could not be read, which is not the same as «off».
    """
    if not ui or str(ui.get("user_alert_low_enabled", "0")).strip().lower() in ("1", "true", "on", "yes"):
        return None
    if is_owner:
        return "/settings?tab=notifications"
    return "/shop-settings?tab=notifications" if can_edit_shop else None


def days_left(expire: datetime, now: datetime) -> float | None:
    """Days until ``expire``; None once it has passed."""
    seconds = (expire - now).total_seconds()
    return seconds / SECONDS_PER_DAY if seconds >= 0 else None


def _template(ui: dict, key: str, default: str) -> str:
    return (ui.get(key) or "").strip() or default


def time_alert_text(ui: dict, *, service_name: str, expire: datetime, now: datetime, renew_button: bool) -> str:
    left = days_left(expire, now)
    body = render_message_template(
        _template(ui, TIME_TEXT_KEY, DEFAULT_TIME_TEXT),
        domain=DOMAIN_USAGE_ALERT,
        service_name=service_name,
        days_left=to_fa_digits(max(0, math.ceil(left or 0))),
        expire_date=to_fa_digits(format_jalali_parts(expire)["date"]),
        shop_title=ui.get("shop_title") or "",
    )
    return f"{body}\n{RENEW_HINT if renew_button else NO_BUTTON_HINT}"


def traffic_alert_text(ui: dict, *, service_name: str, used: float, limit: float) -> str:
    return render_message_template(
        _template(ui, TRAFFIC_TEXT_KEY, DEFAULT_TRAFFIC_TEXT),
        domain=DOMAIN_USAGE_ALERT,
        service_name=service_name,
        traffic_used=format_bytes_ratio(used, limit, joiner=" از "),
        shop_title=ui.get("shop_title") or "",
    )


async def send_alert_with_default_fallback(
    bot: Bot,
    chat_id: int,
    ui: dict,
    build_text: Callable[[dict], str],
    *,
    note: str,
    markup: InlineKeyboardMarkup | None,
) -> Message:
    """Send an alert; when Telegram refuses a shop's custom text (stray ``<``, open tag), send the default.

    Without this a shop whose edited text is not valid HTML would never alert its customers: the send
    fails on every run and the alert is never marked as sent.
    """
    kwargs: dict[str, Any] = {"parse_mode": "HTML", "reply_markup": markup}
    try:
        return await bot.send_message(chat_id, build_text(ui) + note, **kwargs)
    except TelegramBadRequest:
        if not any((ui.get(key) or "").strip() for key in (TIME_TEXT_KEY, TRAFFIC_TEXT_KEY)):
            raise
        logger.warning("custom alert text refused by Telegram; sending the default text", exc_info=True)
        defaults = {**ui, TIME_TEXT_KEY: "", TRAFFIC_TEXT_KEY: ""}
        return await bot.send_message(chat_id, build_text(defaults) + note, **kwargs)
