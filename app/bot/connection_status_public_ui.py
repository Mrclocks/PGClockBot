"""Public monitoring copy contains only escaped names, health, time and vantage."""

from __future__ import annotations

import html
from datetime import datetime

from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.connection_status_ui import STATUS_LABELS, button, checked_time, monitor_text
from app.services.connection_status import PROBE_LABEL, PublicConnectionPage
from app.services.connection_status_policy import is_fresh
from app.services.connection_status_texts import LEGACY_MONITOR_TEXTS, MONITOR_TEXT_DEFAULTS
from app.services.connection_status_types import ConnectionStatus
from app.services.users import get_all_settings


PUBLIC_SCOPE_NOTICE = "این وضعیت فقط دسترسی از سرور ربات را نشان می‌دهد و اتصال شما یا دسترسی از داخل ایران را تأیید نمی‌کند."
CATALOG_STALE_NOTICE = "بررسی موقتاً به‌روز نیست؛ وضعیت فعلی نامشخص است."


class MonitorPublicLabel(BaseFilter):
    async def __call__(self, message: Message, session: AsyncSession) -> bool:
        if not message.text:
            return False
        ui = await get_all_settings(session)
        label = ui["btn_connection_status"].strip()
        accepted = {label}
        if label == MONITOR_TEXT_DEFAULTS["btn_connection_status"]:
            accepted.add(LEGACY_MONITOR_TEXTS["btn_connection_status"])
        return message.text.strip() in accepted


def public_status_text(page: PublicConnectionPage, ui: dict[str, str], *, now: datetime) -> str:
    lines = [f"<b>{monitor_text(ui, 'connection_status_title')}</b>", PROBE_LABEL, PUBLIC_SCOPE_NOTICE]
    if not page.entries:
        lines.append(monitor_text(ui, "connection_status_empty"))
    for entry in page.entries:
        lines.extend(["", f"<b>{html.escape(entry.public_name)}</b>", STATUS_LABELS[entry.status],
                      f"آخرین بررسی: {checked_time(entry.checked_at)}"])
        if entry.is_catalog_stale and entry.status == ConnectionStatus.UNKNOWN:
            lines.append(CATALOG_STALE_NOTICE)
        elif entry.checked_at is not None and not is_fresh(entry.checked_at, now=now, freshness=page.freshness):
            lines.append("نتیجه قدیمی است؛ وضعیت فعلی نامشخص است.")
    return "\n".join(lines)


def public_status_keyboard(page: PublicConnectionPage) -> InlineKeyboardMarkup:
    rows = [[button("بروزرسانی", "csp:refresh")]]
    if page.next_id is not None:
        rows.append([button("صفحهٔ بعد", f"csp:next:{page.next_id}")])
    rows.append([button("بازگشت به منو", "menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def public_cursor(data: str) -> int:
    if data == "csp:refresh":
        return 0
    parts = data.split(":")
    if len(parts) != 3 or parts[:2] != ["csp", "next"]:
        raise ValueError("Invalid public monitoring callback")
    value = parts[2]
    if len(value) > 10 or not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 2147483647:
        raise ValueError("Invalid public monitoring page")
    return int(value)
