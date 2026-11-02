"""Scheduler job: push panel alerts (new version, shop capacity, failed deliveries) to Telegram."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ResellerProfile, ResellerSetting
from app.db.session import SessionLocal
from app.services.notifications import _dispatch_dual_notify
from app.services.replaceable_notices import alert_replace_key, clear_notices
from app.services.reseller_bots import open_notify_bot_for_reseller
from app.services.panel_alert_notifications import (
    DEFAULT_CAPACITY_WARN_PCT,
    KEY_CAPACITY,
    Signal,
    capacity_due,
    capacity_percent,
    capacity_signal,
    claim_signal,
    clear_signal,
    failure_signals,
    mark_capacity_checked,
    release_signal,
    update_signal,
)
from app.services.users import get_all_settings
from app.services.volume_allocation import volume_threshold_signals

logger = logging.getLogger(__name__)

BUTTON_LABEL = "باز کردن در پنل"
CAPACITY_READ_CONCURRENCY = 3
SEND_PAUSE_SECONDS = 0.3


def _markup(path: str) -> InlineKeyboardMarkup | None:
    base = get_settings().public_base_url.rstrip("/")
    if not base.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BUTTON_LABEL, url=base + path)]])


async def announce(bot: Bot, session: AsyncSession, signal: Signal) -> None:
    """Send one signal if its state says it is new; forget it when nobody received it.

    The new alert replaces the previous one of its kind in each chat; an alert that clears
    removes its last message.
    """
    if signal.count <= 0:
        if await clear_signal(session, signal):
            await _clear_alert(bot, session, signal)
        return
    if not await claim_signal(session, signal):
        return
    try:
        delivered = await _dispatch_dual_notify(
            bot, session, signal.notify_key, signal.text,
            markup=_markup(signal.path), ticket_reseller_id=signal.shop_id,
            replace_key=alert_replace_key(signal.alert),
        )
    except (TelegramAPIError, OSError, TimeoutError):
        logger.warning("panel alert delivery failed alert=%s shop=%s", signal.alert, signal.shop_id, exc_info=True)
        delivered = 0
    if not delivered:
        await release_signal(session, signal)
    await asyncio.sleep(SEND_PAUSE_SECONDS)


async def _clear_alert(bot: Bot, session: AsyncSession, signal: Signal) -> None:
    """Remove the last message of a cleared alert, from the bot that sent it."""
    send_bot, should_close = (bot, False)
    if signal.shop_id is not None:
        send_bot, should_close = await open_notify_bot_for_reseller(session, int(signal.shop_id))
        if send_bot is None:
            return
    try:
        await clear_notices(session, send_bot, alert_replace_key(signal.alert))
    finally:
        if should_close:
            await send_bot.session.close()


async def _capacity_shops(session: AsyncSession) -> dict[int, str]:
    """Active shops with a panel admin whose capacity alert is not switched off, as ``{shop: admin}``."""
    profiles = (
        await session.execute(
            select(ResellerProfile.user_id, ResellerProfile.pg_admin_username).where(
                ResellerProfile.is_active.is_(True), ResellerProfile.pg_admin_username.is_not(None)
            )
        )
    ).all()
    off = set(
        (
            await session.scalars(
                select(ResellerSetting.reseller_user_id).where(ResellerSetting.key == KEY_CAPACITY, ResellerSetting.value == "0")
            )
        ).all()
    )
    return {int(uid): str(name) for uid, name in profiles if name and int(uid) not in off}


async def _check_capacity(session: AsyncSession, bot: Bot, shop_id: int, admin_name: str, gate: asyncio.Semaphore) -> None:
    from app.services.pasarguard import get_pg

    async with gate:
        try:
            admin = await get_pg().get_admin(admin_name)
        except Exception:  # noqa: BLE001 - one unreadable admin must not stop the others
            logger.warning("capacity check: admin not readable shop=%s", shop_id, exc_info=True)
            admin = None
    await mark_capacity_checked(session, shop_id)
    if admin is None:
        return
    ui = await get_all_settings(session, reseller_id=shop_id)
    try:
        threshold = float(ui.get("capacity_warn_pct") or DEFAULT_CAPACITY_WARN_PCT)
    except (TypeError, ValueError):
        threshold = DEFAULT_CAPACITY_WARN_PCT
    signal = capacity_signal(shop_id, capacity_percent(admin), threshold)
    if signal is not None:
        await announce(bot, session, signal)


async def run_panel_alert_notifications(bot: Bot) -> None:
    try:
        async with SessionLocal() as session:
            from app.services.updates import check_github_update

            try:
                signal = update_signal(await check_github_update(force=False))
            except Exception:  # noqa: BLE001 - GitHub being unreachable must not stop the other alerts
                logger.warning("panel alerts: update check failed", exc_info=True)
                signal = None
            if signal is not None:
                await announce(bot, session, signal)
            for failure in await failure_signals(session):
                await announce(bot, session, failure)
            shops = await _capacity_shops(session)
            gate = asyncio.Semaphore(CAPACITY_READ_CONCURRENCY)
            for shop_id in await capacity_due(session, list(shops)):
                await _check_capacity(session, bot, shop_id, shops[shop_id], gate)
            for signal in await volume_threshold_signals(session):
                await announce(bot, session, signal)
    except Exception:  # noqa: BLE001 - a failed batch is logged and retried on the next tick
        logger.exception("panel alert notification batch failed")
