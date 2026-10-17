"""Send a service's WireGuard files to its customer, and say so when the panel has none ready."""

from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputMediaDocument
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.notifications import notify_wireguard_missing
from app.services.rich_text import rich_plain_text
from app.services.wireguard_delivery import (
    REASON_DISABLED,
    REASON_UNAVAILABLE,
    WireGuardFetch,
    WireGuardFile,
    fetch_wireguard,
)

logger = logging.getLogger(__name__)

# The panel assigns the peer address in the create call, but the first subscription read can
# still race the new user; one short retry covers that without holding the delivery long.
RETRY_DELAY_SEC = 3.0
ADMIN_ALERT_EVERY_SEC = 6 * 3600
WIREGUARD_BUTTON_KEY = "btn_svc_wireguard"
WIREGUARD_BUTTON_DEFAULT = "🔐 فایل WireGuard"
CAPTION = "🔐 کانفیگ WireGuard"
PARTIAL_TEXT = "🔐 {sent} فایل از {total} فایل WireGuard ارسال شد. بقیه در لینک اشتراک هست."
LATER_TEXT = (
    "🔐 فایل WireGuard این سرویس الان آماده نشد.\n"
    "بعداً از «سرویس‌های من» ← همین سرویس ← «{button}» دریافتش کنید."
)
_ADMIN_TEXTS = {
    REASON_DISABLED: (
        "قالب WireGuard در تنظیمات اشتراک پاسارگارد خاموش است، پس مشتریانی که وایرگارد دارند فایل "
        "دریافت نمی‌کنند. در پنل پاسارگارد، WireGuard را در فرمت‌های اشتراک روشن کنید."
    ),
    REASON_UNAVAILABLE: "پنل پاسارگارد به درخواست فایل WireGuard پاسخ نداد یا خطا داد.",
}
_NO_FILE_ADMIN_TEXT = (
    "سرویس <code>{username}</code> آدرس وایرگارد دارد ولی پنل فایل ‎.conf برایش نداد. "
    "تنظیم هاست WireGuard را در پنل بررسی کنید."
)
# reason -> monotonic time of the last admin alert; at most one entry per reason.
_last_admin_alert: dict[str, float] = {}


def wireguard_button_label(ui: dict[str, str] | None) -> str:
    return rich_plain_text((ui or {}).get(WIREGUARD_BUTTON_KEY)) or WIREGUARD_BUTTON_DEFAULT


async def send_wireguard_files(bot: Bot, chat_id: int, fetched: WireGuardFetch) -> int:
    """Send the files as one document or one album; returns how many reached the chat."""
    files = fetched.files
    if not files:
        return 0
    try:
        if len(files) == 1:
            await bot.send_document(chat_id, document=_input_file(files[0]), caption=CAPTION)
        else:
            album = [
                InputMediaDocument(media=_input_file(item), caption=CAPTION if index == 0 else None)
                for index, item in enumerate(files)
            ]
            await bot.send_media_group(chat_id, media=album)
    except TelegramAPIError:
        logger.warning("wireguard documents send failed chat_id=%s files=%s", chat_id, len(files), exc_info=True)
        return 0
    if fetched.total > len(files):
        await _send_text(bot, chat_id, PARTIAL_TEXT.format(sent=len(files), total=fetched.total))
    return len(files)


async def deliver_wireguard(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    *,
    sub_url: str,
    username: str | None,
    expected: bool,
    ui: dict[str, str] | None,
) -> int:
    """Delivery step: send the files; when the service should have some, retry once, then tell
    the customer where to get them later and warn the platform admin."""
    fetched = await fetch_wireguard(sub_url, username=username)
    if not fetched.files and expected:
        await asyncio.sleep(RETRY_DELAY_SEC)
        fetched = await fetch_wireguard(sub_url, username=username)
    sent = await send_wireguard_files(bot, chat_id, fetched)
    if sent or not expected:
        return sent
    logger.warning("wireguard expected but not delivered reason=%s user=%s", fetched.reason, username)
    await _send_text(bot, chat_id, LATER_TEXT.format(button=html.escape(wireguard_button_label(ui))))
    await _alert_platform_admins(bot, session, fetched.reason, username=username)
    return 0


def _input_file(item: WireGuardFile) -> BufferedInputFile:
    return BufferedInputFile(item.content, filename=item.filename)


async def _send_text(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        logger.warning("wireguard notice send failed chat_id=%s", chat_id, exc_info=True)


def _claim_admin_alert(reason: str) -> bool:
    now = time.monotonic()
    last = _last_admin_alert.get(reason)
    if last is not None and now - last < ADMIN_ALERT_EVERY_SEC:
        return False
    _last_admin_alert[reason] = now
    return True


async def _alert_platform_admins(bot: Bot, session: AsyncSession, reason: str, *, username: str | None) -> None:
    """The panel is the platform's, so only platform admins can fix it; a shop bot cannot reach
    them (they never started it), so alerts go out only from the platform bot."""
    if (getattr(bot, "token", None) or "").strip() != (get_settings().bot_token or "").strip():
        return
    if not _claim_admin_alert(reason):
        return
    text = _ADMIN_TEXTS.get(reason) or _NO_FILE_ADMIN_TEXT.format(username=html.escape(username or "—"))
    await notify_wireguard_missing(bot, session, text)
