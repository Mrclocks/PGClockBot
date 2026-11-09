"""Tell a customer what staff did to their service and why.

The message is queued after the operation and its log record are committed, and a throttled worker
sends it through the bot of the shop that owns the service. A queueing or delivery failure is logged
and never undoes the operation; the staff log keeps the reason either way.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CustomerNotice, UserService
from app.services.action_reasons import log_reasoned_action_as
from app.services.customer_notice_queue import (
    OUTCOME_FAILED,
    OUTCOME_RETRY,
    OUTCOME_SENT,
    OUTCOME_SKIPPED,
    NoticeOutcome,
    add_notice,
)
from app.services.shop_scope import resolve_shop_scope_id
from app.services.staff_audit import StaffActor

logger = logging.getLogger(__name__)

BULK_NOTICE_LIMIT = 500  # one bulk action notifies at most this many services
NOTICE_RECIPIENT_LIMIT = 5  # one panel user is linked to a handful of local rows at most
ACTION_NOTICE_TITLES = {
    "service_renew": "🔄 سرویس شما تمدید شد",
    "service_extend": "⚙️ مانده سرویس شما تغییر کرد",
    "service_delete": "🗑 سرویس شما حذف شد",
    "service_detach": "📤 سرویس از لیست شما جدا شد",
    "pg_user_disable": "⛔ سرویس شما غیرفعال شد",
    "pg_user_enable": "✅ سرویس شما فعال شد",
    "pg_user_reset_usage": "♻️ مصرف سرویس شما صفر شد",
    "pg_user_revoke_sub": "🔗 لینک اشتراک سرویس شما تغییر کرد",
    "pg_user_delete": "🗑 سرویس شما حذف شد",
}
DETACH_HINT = "حساب VPN شما همچنان فعال است؛ فقط دیگر در ربات نمایش داده نمی‌شود."
NEW_LINK_HINT = "لینک قبلی دیگر کار نمی‌کند؛ لینک جدید را از بخش «سرویس‌های من» بگیرید."
ACTION_NOTICE_HINTS = {"pg_user_revoke_sub": NEW_LINK_HINT, "service_detach": DETACH_HINT}


@dataclass(frozen=True, slots=True)
class NoticeTarget:
    """One customer and the service the operation touched."""

    user_id: int
    telegram_id: int
    shop_id: int | None
    service_name: str
    service_id: int | None = None


# Cleanup notices carry the deadline date in ``reason``; {name} and {date} are escaped before use.
CLEANUP_NOTICE_TEXTS = {
    "cleanup_expired": (
        "⌛ <b>سرویس شما منقضی شد</b>\nسرویس: <code>{name}</code>\n"
        "اگر تا <b>{date}</b> تمدید نشود، سرویس حذف می‌شود."
    ),
    "cleanup_warning": (
        "⚠️ <b>هشدار حذف سرویس</b>\nسرویس: <code>{name}</code>\nتاریخ حذف: <b>{date}</b>\n"
        "برای نگه‌داشتن سرویس، پیش از این تاریخ تمدید کنید."
    ),
    "cleanup_volume_finished": (
        "📉 <b>حجم سرویس شما تمام شد</b>\nسرویس: <code>{name}</code>\n"
        "اگر تا <b>{date}</b> تمدید نکنید یا بسته حجم نگیرید، سرویس حذف می‌شود."
    ),
    "cleanup_volume_warning": (
        "⚠️ <b>هشدار حذف سرویس بی‌حجم</b>\nسرویس: <code>{name}</code>\nتاریخ حذف: <b>{date}</b>\n"
        "برای نگه‌داشتن سرویس، پیش از این تاریخ تمدید کنید یا بسته حجم بگیرید."
    ),
}
RENEW_BUTTON_ACTIONS = frozenset(CLEANUP_NOTICE_TEXTS)


def notice_text(action: str, *, service_name: str, reason: str) -> str:
    """The Persian message; every dynamic part is escaped for Telegram HTML."""
    template = CLEANUP_NOTICE_TEXTS.get(action)
    if template is not None:
        return template.format(name=html.escape(service_name or "—"), date=html.escape(reason))
    title = ACTION_NOTICE_TITLES[action]
    lines = [f"<b>{title}</b>", f"سرویس: <code>{html.escape(service_name or '—')}</code>", f"علت: {html.escape(reason)}"]
    hint = ACTION_NOTICE_HINTS.get(action)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


async def pg_user_targets_for_shop(session: AsyncSession, shop_id: int | None, *, pg_user_id: int) -> list[NoticeTarget]:
    """Customers whose local service is the panel user; ``shop_id`` limits it to that shop's services.

    ``None`` means no limit and is only for the platform owner, who may act on every shop.
    """
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


async def pg_users_targets_for_shop(
    session: AsyncSession, shop_id: int | None, *, pg_user_ids: list[int]
) -> dict[int, list[NoticeTarget]]:
    """Notice targets of many panel users in one query, keyed by panel user id (bulk actions)."""
    if not pg_user_ids:
        return {}
    query = (
        select(UserService.pg_user_id, BotUser.id, BotUser.telegram_id, UserService.shop_id, UserService.pg_username)
        .join(UserService, UserService.bot_user_id == BotUser.id)
        .where(UserService.pg_user_id.in_([int(i) for i in pg_user_ids]))
        .limit(BULK_NOTICE_LIMIT)
    )
    if shop_id is not None:
        query = query.where(UserService.shop_id == int(shop_id))
    grouped: dict[int, list[NoticeTarget]] = {}
    for pg_id, uid, tg, shop, name in (await session.execute(query)).all():
        if tg:
            grouped.setdefault(int(pg_id), []).append(NoticeTarget(int(uid), int(tg), (int(shop) if shop else None), str(name or "")))
    return grouped


async def pg_user_targets(session: AsyncSession, staff: dict, *, pg_user_id: int) -> list[NoticeTarget]:
    """``pg_user_targets_for_shop`` for a web staff session, limited to the acting shop."""
    return await pg_user_targets_for_shop(session, resolve_shop_scope_id(staff), pg_user_id=pg_user_id)


async def notify_customers(session: AsyncSession, targets: list[NoticeTarget], *, action: str, reason: str) -> int:
    """Queue the notice for each target (sent later by the throttled worker); returns how many were queued.

    A queueing failure is logged and never undoes the operation that was already committed.
    """
    if not targets:
        return 0
    try:
        for target in targets:
            add_notice(
                session,
                shop_id=target.shop_id,
                bot_user_id=target.user_id,
                telegram_id=target.telegram_id,
                action=action,
                service_name=target.service_name,
                reason=reason,
                service_id=target.service_id,
            )
        await session.commit()
    except Exception:  # noqa: BLE001 - the operation succeeded; only the customer message is lost
        logger.warning("customer notice queueing failed action=%s", action, exc_info=True)
        await session.rollback()
        return 0
    return len(targets)


BotFor = Callable[[int | None], Awaitable[Bot | None]]


def _renew_markup(notice: CustomerNotice) -> InlineKeyboardMarkup | None:
    """A renew button on the warnings that tell the customer a service will be deleted."""
    if notice.action not in RENEW_BUTTON_ACTIONS or not notice.service_id:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 تمدید سرویس", callback_data=f"svc:renew:{int(notice.service_id)}")]]
    )


async def deliver_notice(session: AsyncSession, bot_for: BotFor, notice: CustomerNotice) -> NoticeOutcome:
    """Send one queued notice through its shop's bot and say what happened to it."""
    user = await session.get(BotUser, int(notice.bot_user_id))
    if user is None or user.is_blocked:
        return NoticeOutcome(OUTCOME_SKIPPED)
    bot = await bot_for(notice.shop_id)
    if bot is None:
        logger.warning("customer notice skipped: no bot for shop=%s", notice.shop_id)
        return NoticeOutcome(OUTCOME_SKIPPED)
    text = notice_text(notice.action, service_name=notice.service_name, reason=notice.reason)
    try:
        await bot.send_message(int(notice.telegram_id), text, parse_mode="HTML", reply_markup=_renew_markup(notice))
    except TelegramRetryAfter as exc:
        return NoticeOutcome(OUTCOME_RETRY, timedelta(seconds=int(exc.retry_after) + 1))
    except TelegramForbiddenError:
        return NoticeOutcome(OUTCOME_SKIPPED)
    except TelegramBadRequest:
        logger.warning("customer notice rejected id=%s", notice.id, exc_info=True)
        return NoticeOutcome(OUTCOME_FAILED)
    except (TelegramAPIError, OSError, TimeoutError):
        logger.warning("customer notice failed id=%s", notice.id, exc_info=True)
        return NoticeOutcome(OUTCOME_RETRY)
    return NoticeOutcome(OUTCOME_SENT)


async def log_and_notify(
    session: AsyncSession,
    actor: StaffActor,
    *,
    action: str,
    target_type: str,
    target_id: int | None,
    reason: str,
    targets: list[NoticeTarget],
    notice_action: str | None = None,
) -> None:
    """Record a finished reasoned operation in the staff log, then tell the affected customers."""
    await log_reasoned_action_as(
        session, actor, action=action, target_type=target_type, target_id=target_id, reason=reason
    )
    await notify_customers(session, targets, action=notice_action or action, reason=reason)
