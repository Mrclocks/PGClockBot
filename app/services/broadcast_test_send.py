"""Test send of a composed group message to the sender's own Telegram account before queueing it.

The message goes through the tenant's own bot with the same text rules and buttons the worker uses, so the sender
sees exactly what customers will get. On the platform it goes to the bot admins (``ADMIN_IDS``, the owner's
accounts); in a shop to the shop owner's account through the shop's bot. Nothing is queued or recorded. Telegram's
refusal is turned into a safe explanation; its own text is only logged. A chosen photo or video is sent from
memory with the text as its caption; it is not stored.
"""

from __future__ import annotations

import html
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser
from app.services.broadcast_failures import KIND_BAD_MESSAGE, failure_kind
from app.services.broadcast_media import KIND_PHOTO, check_media_upload
from app.services.broadcast_queue import clean_platform_text
from app.services.message_buttons import stored_keyboard
from app.services.shop_broadcast import SHOP_NO_BOT_MESSAGE, clean_shop_broadcast_text

logger = logging.getLogger(__name__)

TEST_RECIPIENT_LIMIT = 3
TEST_TIMEOUT_SECONDS = 15
NO_RECIPIENT = "آیدی تلگرامی برای ارسال آزمایشی پیدا نشد؛ آیدی ادمین‌ها را در تنظیمات ربات وارد کنید"
NOT_REACHABLE = "ربات نمی‌تواند به حساب شما پیام بدهد؛ اول ربات را از همان حساب استارت کنید"
REJECTED = "تلگرام پیام را نپذیرفت؛ متن، لینک‌ها و دکمه‌ها را بررسی کنید"
SLOW_DOWN = "تلگرام کمی صبر خواست؛ چند ثانیه بعد دوباره امتحان کنید"
NOT_SENT = "ارسال آزمایشی انجام نشد؛ چند لحظه بعد دوباره امتحان کنید"

BotOpener = Callable[[AsyncSession, int | None], AbstractAsyncContextManager[Bot | None]]


@dataclass(frozen=True, slots=True)
class TestMedia:
    """A validated photo or video to send with the test, kept in memory only."""

    kind: str
    extension: str
    data: bytes


def checked_test_media(data: bytes) -> TestMedia:
    """Check an uploaded photo or video; raises ``ValueError`` with a safe reason."""
    kind, extension = check_media_upload(data)
    return TestMedia(kind=kind, extension=extension, data=data)


@dataclass(frozen=True, slots=True)
class TestMessage:
    body: str
    markup: InlineKeyboardMarkup | None
    media: TestMedia | None = None


@asynccontextmanager
async def tenant_bot(session: AsyncSession, shop_id: int | None) -> AsyncIterator[Bot | None]:
    """The bot the tenant's group messages go through (None when a shop has no usable bot)."""
    # Deferred: importing app.bot / reseller_bots pulls every handler module, which this service does not need.
    from app.bot import create_bot
    from app.services.reseller_bots import open_notify_bot_for_reseller

    if shop_id is None:
        bot, should_close = create_bot(), True
    else:
        bot, should_close = await open_notify_bot_for_reseller(session, int(shop_id))
    try:
        yield bot
    finally:
        if should_close and bot is not None:
            await bot.session.close()


async def test_recipients(session: AsyncSession, *, shop_id: int | None) -> list[int]:
    """Telegram ids that get the test: the bot admins, or the shop owner."""
    if shop_id is None:
        return [int(admin_id) for admin_id in get_settings().admin_ids[:TEST_RECIPIENT_LIMIT]]
    owner = await session.scalar(select(BotUser.telegram_id).where(BotUser.id == int(shop_id)))
    return [int(owner)] if owner else []


async def build_test_message(
    session: AsyncSession, *, shop_id: int | None, text: object, buttons: str | None, media: TestMedia | None = None
) -> TestMessage:
    """The body, buttons and media exactly as the worker would send them for this tenant."""
    with_media = media is not None
    if shop_id is None:
        body = clean_platform_text(str(text or ""), with_media=with_media)
    else:
        body = html.escape(clean_shop_broadcast_text(text, with_media=with_media), quote=False)
    keyboard = await stored_keyboard(session, buttons, shop_id=shop_id)
    return TestMessage(body, InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None, media)


def _refusal(exc: TelegramBadRequest | TelegramForbiddenError) -> str:
    return REJECTED if failure_kind(exc) == KIND_BAD_MESSAGE else NOT_REACHABLE


async def _deliver(bot: Bot, chat_id: int, message: TestMessage) -> str | None:
    """None when delivered, else the safe reason it was not."""
    try:
        await _send_one(bot, chat_id, message)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.info("broadcast test send refused: %s", exc)
        return _refusal(exc)
    except TelegramRetryAfter:
        return SLOW_DOWN
    except Exception:
        logger.warning("broadcast test send failed", exc_info=True)
        return NOT_SENT
    return None


async def _send_one(bot: Bot, chat_id: int, message: TestMessage) -> None:
    options = {"reply_markup": message.markup, "request_timeout": TEST_TIMEOUT_SECONDS}
    media = message.media
    if media is None:
        await bot.send_message(chat_id, message.body, parse_mode="HTML", **options)
        return
    caption = {"caption": message.body, "parse_mode": "HTML"} if message.body else {}
    file = BufferedInputFile(media.data, filename=f"preview.{media.extension}")
    if media.kind == KIND_PHOTO:
        await bot.send_photo(chat_id, file, **caption, **options)
    else:
        await bot.send_video(chat_id, file, supports_streaming=True, **caption, **options)


async def send_test_broadcast(
    session: AsyncSession,
    *,
    shop_id: int | None,
    text: object,
    buttons: str | None,
    open_bot: BotOpener | None = None,
    media: TestMedia | None = None,
) -> int:
    """Send the composed message to the sender's own account(s); returns how many got it.

    Raises ``ValueError`` with a safe message when the text is invalid or nobody could get it.
    """
    message = await build_test_message(session, shop_id=shop_id, text=text, buttons=buttons, media=media)
    recipients = await test_recipients(session, shop_id=shop_id)
    if not recipients:
        raise ValueError(NO_RECIPIENT)
    async with (open_bot or tenant_bot)(session, shop_id) as bot:
        if bot is None:
            raise ValueError(SHOP_NO_BOT_MESSAGE)
        # Only reads happened; end the transaction so no database work waits on Telegram.
        await session.commit()
        reasons = [await _deliver(bot, chat_id, message) for chat_id in recipients]
    delivered = reasons.count(None)
    if not delivered:
        raise ValueError(next(reason for reason in reasons if reason))
    return delivered
