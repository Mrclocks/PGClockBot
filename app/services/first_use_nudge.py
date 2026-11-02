"""One helpful message for a service nobody has used a day after it was delivered.

A customer who bought but never connected is the commonest support ticket. For services delivered
24 to 72 hours ago the check reads the panel usage once; when it is still zero (or the service is
waiting for its first connection) the customer gets one message with the connection guide and a way
to reach support. The service is stamped before anything is sent, so the message is at-most-once.
"""
from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, UserService
from app.services.users import get_setting, on

logger = logging.getLogger(__name__)

MIN_AGE_HOURS = 24
MAX_AGE_HOURS = 72
BATCH_SIZE = 100
MAX_BATCHES = 5
FETCH_CONCURRENCY = 8
FETCH_TIMEOUT_SECONDS = 12
SETTING_KEY = "first_use_nudge_enabled"
UNUSED_STATUSES = frozenset({"active", "on_hold"})
GUIDE_CALLBACK = "guide:svc:{service_id}"
SUPPORT_CALLBACK = "nv:s:new"


def nudge_text(service_name: str) -> str:
    return (
        f"👋 سرویس <b>{html.escape(service_name)}</b> هنوز مصرفی نداشته است.\n"
        "اگر در اتصال به مشکل خورده‌اید، آموزش اتصال را ببینید؛ اگر باز هم وصل نشد، به پشتیبانی پیام بدهید."
    )


def nudge_markup(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 آموزش اتصال", callback_data=GUIDE_CALLBACK.format(service_id=service_id))],
            [InlineKeyboardButton(text="✉️ پیام به پشتیبانی", callback_data=SUPPORT_CALLBACK)],
        ]
    )


def is_unused(info: dict[str, Any]) -> bool:
    """True when the panel says the service has never carried traffic."""
    status = str(info.get("status") or "").strip().lower().replace("-", "_")
    try:
        used = float(info.get("used_traffic") or 0)
    except (TypeError, ValueError):
        return False
    return status in UNUSED_STATUSES and used <= 0


async def _candidates(session: AsyncSession, now: datetime, after_id: int) -> list[UserService]:
    return list(
        await session.scalars(
            select(UserService)
            .where(
                UserService.id > after_id,
                UserService.first_use_checked_at.is_(None),
                UserService.created_at <= now - timedelta(hours=MIN_AGE_HOURS),
                UserService.created_at > now - timedelta(hours=MAX_AGE_HOURS),
                UserService.subscription_token.is_not(None),
                UserService.remark.like("order:%"),
                UserService.is_cancelled.is_(False),
                UserService.cancellation_pending.is_(False),
                UserService.shop_needs_review.is_(False),
            )
            .order_by(UserService.id)
            .limit(BATCH_SIZE)
        )
    )


async def _stamp(session: AsyncSession, service_id: int, now: datetime) -> bool:
    """Claim the service for this run; False when another run already stamped it."""
    claim = await session.execute(
        update(UserService)
        .where(UserService.id == service_id, UserService.first_use_checked_at.is_(None))
        .values(first_use_checked_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return claim.rowcount == 1


async def _fetch_all(pg, services: list[UserService]) -> list[dict[str, Any] | None]:
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(token: str) -> dict[str, Any] | None:
        async with gate:
            try:
                info = await asyncio.wait_for(pg.subscription_info(token), timeout=FETCH_TIMEOUT_SECONDS)
            except Exception:
                # Best effort: the panel being slow only delays the check to the next run.
                logger.debug("first-use usage lookup failed", exc_info=True)
                return None
            return info if isinstance(info, dict) else None

    return await asyncio.gather(*(fetch(service.subscription_token) for service in services))


async def _eligible(
    session: AsyncSession, services: list[UserService], send_bot_for: Callable[[int | None], Bot | None]
) -> list[tuple[UserService, BotUser, Bot]]:
    owners = {
        int(user.id): user
        for user in await session.scalars(select(BotUser).where(BotUser.id.in_({service.bot_user_id for service in services})))
    }
    enabled: dict[int | None, bool] = {}
    result = []
    for service in services:
        shop = int(service.shop_id) if service.shop_id else None
        if shop not in enabled:
            enabled[shop] = on(await get_setting(session, SETTING_KEY, "0", reseller_id=shop))
        user, bot = owners.get(int(service.bot_user_id)), send_bot_for(shop)
        if enabled[shop] and user is not None and not user.is_blocked and bot is not None:
            result.append((service, user, bot))
    return result


async def _send(bot: Bot, user: BotUser, service: UserService) -> bool:
    try:
        await bot.send_message(user.telegram_id, nudge_text(service.pg_username), parse_mode="HTML", reply_markup=nudge_markup(service.id))
    except (TelegramForbiddenError, TelegramBadRequest):
        return False
    except Exception:
        # The service is already stamped, so an unknown outcome is not retried.
        logger.warning("first-use message not confirmed for service %s", service.id, exc_info=True)
        return False
    return True


async def run_first_use_check(
    session: AsyncSession, pg, send_bot_for: Callable[[int | None], Bot | None], *, now: datetime | None = None
) -> int:
    """Check one bounded slice of new services and message the customers whose service is unused."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    after_id = 0
    for _ in range(MAX_BATCHES):
        services = await _candidates(session, now, after_id)
        if not services:
            break
        after_id = int(services[-1].id)
        eligible = await _eligible(session, services, send_bot_for)
        infos = await _fetch_all(pg, [service for service, _, _ in eligible])
        for (service, user, bot), info in zip(eligible, infos, strict=True):
            if info is None or not await _stamp(session, int(service.id), now):
                continue
            if is_unused(info) and await _send(bot, user, service):
                sent += 1
        if len(services) < BATCH_SIZE:
            break
    return sent
