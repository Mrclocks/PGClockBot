"""Tell a customer what staff did to their service and why.

The message is sent after the operation and its log record are committed, through the bot of the
shop that owns the service. A delivery failure is logged and never undoes the operation; the
staff log keeps the reason either way.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, UserService
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user
from app.services.shop_scope import resolve_shop_scope_id

logger = logging.getLogger(__name__)

NOTICE_RECIPIENT_LIMIT = 20  # one panel user is linked to a handful of local rows at most
ACTION_NOTICE_TITLES = {
    "service_renew": "🔄 سرویس شما تمدید شد",
    "service_extend": "⚙️ مانده سرویس شما تغییر کرد",
    "service_delete": "🗑 سرویس شما حذف شد",
    "pg_user_disable": "⛔ سرویس شما غیرفعال شد",
    "pg_user_enable": "✅ سرویس شما فعال شد",
    "pg_user_reset_usage": "♻️ مصرف سرویس شما صفر شد",
    "pg_user_revoke_sub": "🔗 لینک اشتراک سرویس شما تغییر کرد",
    "pg_user_delete": "🗑 سرویس شما حذف شد",
}
NEW_LINK_HINT = "لینک قبلی دیگر کار نمی‌کند؛ لینک جدید را از بخش «سرویس‌های من» بگیرید."
ACTION_NOTICE_HINTS = {"pg_user_revoke_sub": NEW_LINK_HINT}


@dataclass(frozen=True, slots=True)
class NoticeTarget:
    """One customer and the service the operation touched."""

    user_id: int
    telegram_id: int
    shop_id: int | None
    service_name: str


def notice_text(action: str, *, service_name: str, reason: str) -> str:
    """The Persian message; every dynamic part is escaped for Telegram HTML."""
    title = ACTION_NOTICE_TITLES[action]
    lines = [f"<b>{title}</b>", f"سرویس: <code>{html.escape(service_name or '—')}</code>", f"علت: {html.escape(reason)}"]
    hint = ACTION_NOTICE_HINTS.get(action)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


async def pg_user_targets(session: AsyncSession, staff: dict, *, pg_user_id: int) -> list[NoticeTarget]:
    """Customers whose local service is the panel user, limited to the acting shop's own services."""
    shop_id = resolve_shop_scope_id(staff)
    query = (
        select(BotUser.id, BotUser.telegram_id, UserService.shop_id, UserService.pg_username)
        .join(UserService, UserService.bot_user_id == BotUser.id)
        .where(UserService.pg_user_id == int(pg_user_id))
        .limit(NOTICE_RECIPIENT_LIMIT)
    )
    if shop_id is not None:
        query = query.where(UserService.shop_id == int(shop_id))
    rows = (await session.execute(query)).all()
    return [NoticeTarget(int(uid), int(tg), (int(shop) if shop else None), str(name or "")) for uid, tg, shop, name in rows if tg]


async def notify_customers(session: AsyncSession, targets: list[NoticeTarget], *, action: str, reason: str) -> int:
    """Send the notice to each target; returns how many were delivered."""
    delivered = 0
    for target in targets:
        user = await session.get(BotUser, target.user_id)
        if user is None or user.is_blocked:
            continue
        try:
            bot, should_close = await open_notify_bot_for_user(session, user, shop_id=target.shop_id)
        except (ShopBotUnavailable, TokenValidationError):
            logger.warning("customer notice skipped: bot unavailable shop=%s", target.shop_id, exc_info=True)
            continue
        try:
            await bot.send_message(
                target.telegram_id,
                notice_text(action, service_name=target.service_name, reason=reason),
                parse_mode="HTML",
            )
            delivered += 1
        except (TelegramAPIError, OSError, TimeoutError):
            logger.warning("customer notice failed action=%s user=%s", action, target.user_id, exc_info=True)
        finally:
            if should_close:
                await bot.session.close()
    return delivered
