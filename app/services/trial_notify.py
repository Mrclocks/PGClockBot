"""Tell a shop's staff (or the platform admins) that a customer received a free trial."""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, Plan
from app.services.formatting import format_message, info_block, kv_line
from app.services.notifications import _dispatch_dual_notify

logger = logging.getLogger(__name__)

NOTIFY_KEY = "notify_trial_claimed"
NO_USERNAME = "ندارد"


def trial_claimed_text(*, user: BotUser, plan_name: str, order_id: int) -> str:
    username = (user.username or "").strip().lstrip("@")
    return format_message(
        "🧪 دریافت اکانت تست",
        info_block([
            kv_line("👤", "نام", html.escape((user.full_name or "").strip() or "—")),
            kv_line("🔗", "یوزرنیم", f"@{html.escape(username)}" if username else NO_USERNAME),
            kv_line("🆔", "آیدی تلگرام", f"<code>{int(user.telegram_id or 0)}</code>"),
            kv_line("📦", "پلن", html.escape(plan_name or "—")),
            kv_line("🛒", "سفارش", f"#{int(order_id)}"),
        ]),
    )


async def notify_trial_claimed(bot: Bot, session: AsyncSession, *, order: Order, user: BotUser) -> int:
    """Notify the order's shop (or the platform admins) of a delivered trial; returns chats reached.

    Does nothing for a non-trial or undelivered order. Best effort: the trial is already
    delivered, so a Telegram failure never fails the customer's flow.
    """
    if not order.plan_id or order.status != OrderStatus.DELIVERED.value:
        return 0
    row = (await session.execute(select(Plan.name, Plan.is_trial).where(Plan.id == int(order.plan_id)))).one_or_none()
    if row is None or not row.is_trial:
        return 0
    text = trial_claimed_text(user=user, plan_name=row.name, order_id=int(order.id))
    try:
        return await _dispatch_dual_notify(bot, session, NOTIFY_KEY, text, order=order)
    except (TelegramAPIError, OSError, TimeoutError):
        logger.warning("trial claimed notice failed order=%s", order.id, exc_info=True)
        return 0


async def notify_trial_claimed_outside_bot(session: AsyncSession, *, order: Order, user: BotUser) -> int:
    """Same notice for flows without a live bot (Mini App): opens and closes the order's bot."""
    # Local import: reseller_bots pulls in the bot dispatcher, as in notifications.
    from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user

    try:
        bot, should_close = await open_notify_bot_for_user(session, user, shop_id=order.reseller_id)
    except ShopBotUnavailable:
        logger.warning("trial claimed notice: no shop bot order=%s", order.id)
        return 0
    try:
        return await notify_trial_claimed(bot, session, order=order, user=user)
    finally:
        if should_close:
            await bot.session.close()
