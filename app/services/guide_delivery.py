"""Delivering connection guides: the album sender and the Mini App's guide view and send-to-chat."""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, Message

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.formatting import format_message
from app.services.guide_media import KIND_VIDEO, MediaToSend, load_file_id_cache, media_to_send, remember_file_ids
from app.services.rich_text import visible_text
from app.services.chat_records import keeps_sent

logger = logging.getLogger(__name__)


def _album_item(item: MediaToSend) -> InputMediaPhoto | InputMediaVideo:
    source = item.file_id or FSInputFile(item.path)
    return InputMediaVideo(media=source) if item.kind == KIND_VIDEO else InputMediaPhoto(media=source)


def _sent_file_id(message: Message) -> str | None:
    if message.video:
        return message.video.file_id
    return message.photo[-1].file_id if message.photo else None


@keeps_sent
async def send_guide_album(bot: Bot, chat_id: int, album: list[MediaToSend]) -> dict[str, str]:
    """Send the guide's photos and videos; returns the file ids learned for uploaded files."""
    try:
        if len(album) == 1:
            item = album[0]
            source = item.file_id or FSInputFile(item.path)
            sent = [
                await (bot.send_video(chat_id, video=source) if item.kind == KIND_VIDEO else bot.send_photo(chat_id, photo=source))
            ]
        else:
            sent = await bot.send_media_group(chat_id, media=[_album_item(item) for item in album])
    except TelegramAPIError:
        logger.warning("guide album send failed chat_id=%s items=%s", chat_id, len(album), exc_info=True)
        return {}
    learned = {}
    for item, message in zip(album, sent, strict=False):
        file_id = _sent_file_id(message)
        if item.path is not None and file_id:
            learned[item.media_id] = file_id
    return learned



# The Mini App shows guides as text; photos and videos are sent to the customer's chat.
GUIDE_SEND_INTERVAL_SECONDS = 60.0
_TRACKED_SENDERS = 5000
_recent_sends: OrderedDict[int, float] = OrderedDict()
_GUIDE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def guide_cards(guides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Customer guides as plain data for a non-Telegram client (no file names or file ids)."""
    return [
        {
            "id": str(g["id"]),
            "title": str(g.get("title") or "آموزش"),
            "body": visible_text(g.get("body")),
            "links": [{"label": link["label"], "url": link["url"]} for link in g.get("links") or []],
            "opens_subscription": bool(g.get("deep_link")),
            "media_count": len(g.get("media") or []),
        }
        for g in guides
    ]


def find_customer_guide(guides: list[dict[str, Any]], guide_id: object) -> dict[str, Any] | None:
    if not isinstance(guide_id, str) or not _GUIDE_ID.match(guide_id):
        return None
    return next((g for g in guides if str(g.get("id")) == guide_id), None)


def claim_guide_send_slot(user_id: int) -> bool:
    """False when this customer had a guide sent within the interval."""
    now = time.monotonic()
    last = _recent_sends.get(int(user_id))
    if last is not None and now - last < GUIDE_SEND_INTERVAL_SECONDS:
        return False
    _recent_sends[int(user_id)] = now
    _recent_sends.move_to_end(int(user_id))
    while len(_recent_sends) > _TRACKED_SENDERS:
        _recent_sends.popitem(last=False)
    return True


@keeps_sent
async def send_guide_to_chat(
    session: AsyncSession,
    bot: Bot,
    *,
    chat_id: int,
    guide: dict[str, Any],
    all_guides: list[dict[str, Any]],
    shop_id: int | None = None,
) -> None:
    """The guide's album (if any) and its text, as the bot shows it; ``shop_id``'s guides (None = platform)."""
    cache = await load_file_id_cache(session, reseller_id=shop_id)
    album = media_to_send(guide, bot_id=bot.id, cache=cache, reseller_id=shop_id)
    if album:
        learned = await send_guide_album(bot, chat_id, album)
        if learned:
            live = {m["id"] for g in all_guides for m in g.get("media") or []}
            await remember_file_ids(session, learned, bot_id=bot.id, live_media_ids=live, reseller_id=shop_id)
    body = (guide.get("body") or "").strip() or "—"
    await bot.send_message(chat_id, format_message(f"📘 {guide.get('title') or 'آموزش'}", body))
