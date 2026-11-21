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
from app.services.auto_renew_offer import ACTION as AUTO_RENEW_OFFER, OFFER_TEXT as AUTO_RENEW_OFFER_TEXT
from app.services.service_freeze_settings import RESUMED_ACTION as FREEZE_RESUMED, RESUMED_TEXT as FREEZE_RESUMED_TEXT
from app.services.service_transfer_settings import DONE_ACTION as TRANSFER_DONE, DONE_TEXT as TRANSFER_DONE_TEXT
from app.services.customer_notice_buttons import (
    auto_renew_button,
    buy_new_button,
    help_row,
    renew_button,
    services_button,
    support_button,
    volume_pack_button,
)
from app.services.customer_notice_texts import CLEANUP_ACTION_LABELS, notice_text_key
from app.services.message_variables import DOMAIN_SERVICE_NOTICE, render_message_template
from app.services.shop_scope import resolve_shop_scope_id
from app.services.staff_audit import StaffActor
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

BULK_NOTICE_LIMIT = 500  # one bulk action notifies at most this many services
NOTICE_RECIPIENT_LIMIT = 5  # one panel user is linked to a handful of local rows at most
ACTION_NOTICE_TITLES = {
    "service_renew": "🔄 سرویس شما تمدید شد",
    "service_extend": "⚙️ مانده سرویس شما تغییر کرد",
    "service_delete": "🗑 سرویس شما حذف شد",
    "service_detach": "📤 سرویس از لیست شما جدا شد",
    "service_import_assign": "➕ سرویس قبلی به «سرویس‌های من» شما اضافه شد",
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
    "cleanup_deleted": (
        "🗑 <b>سرویس شما حذف شد</b>\nسرویس: <code>{name}</code>\n"
        "این سرویس منقضی شده بود و در مهلت تعیین‌شده تمدید نشد. برای ادامه می‌توانید سرویس جدید بخرید."
    ),
    "cleanup_volume_deleted": (
        "🗑 <b>سرویس شما حذف شد</b>\nسرویس: <code>{name}</code>\n"
        "حجم این سرویس تمام شده بود و در مهلت تعیین‌شده تمدید نشد. برای ادامه می‌توانید سرویس جدید بخرید."
    ),
}
# Only the warnings that leave the customer a way out carry the renew button.
RENEW_BUTTON_ACTIONS = frozenset(
    {"cleanup_expired", "cleanup_warning", "cleanup_volume_finished", "cleanup_volume_warning"}
)
# The volume warnings may also offer a pack, when the shop sells one.
VOLUME_PACK_ACTIONS = frozenset({"cleanup_volume_finished", "cleanup_volume_warning"})
# The service is gone: offer a new one instead of the customer's service list.
SERVICE_GONE_ACTIONS = frozenset(
    {"cleanup_deleted", "cleanup_volume_deleted", "service_delete", "pg_user_delete", "service_detach"}
)
# The customer needs their service list (a new link, a service now in the list) more than support.
SERVICES_ONLY_ACTIONS = frozenset({"pg_user_revoke_sub", "service_import_assign"})


def notice_text(action: str, *, service_name: str, reason: str) -> str:
    """The Persian message; every dynamic part is escaped for Telegram HTML."""
    fixed = {
        AUTO_RENEW_OFFER: AUTO_RENEW_OFFER_TEXT, FREEZE_RESUMED: FREEZE_RESUMED_TEXT, TRANSFER_DONE: TRANSFER_DONE_TEXT,
    }.get(action)
    if fixed is not None:
        return fixed.format(name=html.escape(service_name or "—"))
    template = CLEANUP_NOTICE_TEXTS.get(action)
    if template is not None:
        return template.format(name=html.escape(service_name or "—"), date=html.escape(reason or "—"))
    title = ACTION_NOTICE_TITLES[action]
    lines = [f"<b>{title}</b>", f"سرویس: <code>{html.escape(service_name or '—')}</code>", f"علت: {html.escape(reason)}"]
    hint = ACTION_NOTICE_HINTS.get(action)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def custom_notice_text(ui: dict, action: str, *, service_name: str, reason: str) -> str | None:
    """The shop's own text for this notice, or None to keep the built-in one.

    Values are escaped by ``render_message_template``; the template itself is the shop's own HTML.
    """
    template = (ui.get(notice_text_key(action)) or "").strip()
    if not template:
        return None
    is_cleanup = action in CLEANUP_ACTION_LABELS
    return render_message_template(
        template,
        domain=DOMAIN_SERVICE_NOTICE,
        service_name=service_name or "—",
        reason="" if is_cleanup else (reason or ""),
        delete_date=(reason or "—") if is_cleanup else "",
        shop_title=ui.get("shop_title") or "",
    )


async def _notice_settings(session: AsyncSession, shop_id: int | None) -> dict:
    """The owning shop's settings; 0 pins the platform's when the notice belongs to no shop."""
    return await get_all_settings(session, reseller_id=int(shop_id) if shop_id else 0)


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
PacksFor = Callable[[int | None], Awaitable[bool]]


def _way_out_rows(notice: CustomerNotice, *, sells_volume_packs: bool) -> list[list[InlineKeyboardButton]]:
    """What the customer can do about the service: a pack, a renewal, or a new service."""
    if notice.action in SERVICE_GONE_ACTIONS:
        return [[buy_new_button()]]
    if not notice.service_id:
        return []
    if notice.action == AUTO_RENEW_OFFER:
        return [[auto_renew_button(int(notice.service_id))]]
    rows = []
    if notice.action in VOLUME_PACK_ACTIONS and sells_volume_packs:
        rows.append([volume_pack_button(int(notice.service_id))])
    if notice.action in RENEW_BUTTON_ACTIONS:
        rows.append([renew_button(int(notice.service_id))])
    return rows


def notice_markup_for(notice: CustomerNotice, *, sells_volume_packs: bool = False) -> InlineKeyboardMarkup:
    """The buttons under one customer notice; every notice ends with its help row."""
    rows = _way_out_rows(notice, sells_volume_packs=sells_volume_packs)
    if notice.action in SERVICES_ONLY_ACTIONS:
        rows.append([services_button()])
    elif notice.action in SERVICE_GONE_ACTIONS:
        rows.append([support_button()])
    else:
        rows.append(help_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def deliver_notice(
    session: AsyncSession, bot_for: BotFor, notice: CustomerNotice, *, packs_for: PacksFor | None = None
) -> NoticeOutcome:
    """Send one queued notice through its shop's bot and say what happened to it.

    ``packs_for`` says whether a shop sells volume packs; the job memoises it per batch.
    """
    user = await session.get(BotUser, int(notice.bot_user_id))
    if user is None or user.is_blocked:
        return NoticeOutcome(OUTCOME_SKIPPED)
    bot = await bot_for(notice.shop_id)
    if bot is None:
        logger.warning("customer notice skipped: no bot for shop=%s", notice.shop_id)
        return NoticeOutcome(OUTCOME_SKIPPED)
    default_text = notice_text(notice.action, service_name=notice.service_name, reason=notice.reason)
    custom_text = custom_notice_text(
        await _notice_settings(session, notice.shop_id),
        notice.action,
        service_name=notice.service_name,
        reason=notice.reason,
    )
    sells_packs = bool(packs_for and notice.action in VOLUME_PACK_ACTIONS and await packs_for(notice.shop_id))
    markup = notice_markup_for(notice, sells_volume_packs=sells_packs)
    try:
        await _send_with_default_fallback(
            bot, int(notice.telegram_id), custom_text, default_text, markup=markup, notice_id=notice.id
        )
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


async def _send_with_default_fallback(
    bot: Bot,
    chat_id: int,
    custom_text: str | None,
    default_text: str,
    *,
    markup: InlineKeyboardMarkup,
    notice_id: int | None,
) -> None:
    """Send the shop's text; when Telegram refuses it (stray ``<``, open tag), send the built-in text.

    Without this a shop whose edited text is not valid HTML would lose every notice of that kind.
    """
    if custom_text is None:
        await bot.send_message(chat_id, default_text, parse_mode="HTML", reply_markup=markup)
        return
    try:
        await bot.send_message(chat_id, custom_text, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest:
        logger.warning("custom notice text refused by Telegram id=%s; sending the default", notice_id, exc_info=True)
        await bot.send_message(chat_id, default_text, parse_mode="HTML", reply_markup=markup)


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
