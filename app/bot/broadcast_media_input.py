"""Group-message input in the bot: read a photo or video with its caption, and preview it as customers get it.

The owner's (``admin_broadcast_confirm``) and a shop's (``handlers/shop_broadcast``) flows share these helpers.
Media is kept only as the receiving bot's file id, which is the bot that later sends the broadcast.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, Message

from app.services.broadcast_media import KIND_PHOTO, KIND_VIDEO, BroadcastMedia, media_from_row

MEDIA_DATA_KEY = "bc_media"
TEXT_PROMPT = "متن، عکس یا ویدیو:"
ONLY_TEXT_OR_MEDIA = "⚠️ فقط متن، یا یک عکس یا ویدیو (با زیرنویس اختیاری) قابل ارسال گروهی است. دوباره بفرستید."
SENT_AS_FILE = (
    "⚠️ این {kind} به‌صورت «فایل» رسید. آن را به‌صورت {kind} معمولی بفرستید (گزینهٔ ارسال به‌عنوان فایل را نزنید)؛ "
    "زیرنویس اختیاری است."
)
GIF_NOT_SUPPORTED = "⚠️ گیف قابل ارسال گروهی نیست؛ آن را به‌صورت ویدیو بفرستید."


def media_from_message(message: Message) -> BroadcastMedia | None:
    """The photo (largest size) or video in the message; ``None`` for anything else."""
    if message.photo:
        return BroadcastMedia(kind=KIND_PHOTO, file_id=message.photo[-1].file_id)
    if message.video:
        return BroadcastMedia(kind=KIND_VIDEO, file_id=message.video.file_id)
    return None


def refusal_text(message: Message) -> str:
    """Why this message cannot be a group message, and how to send it instead."""
    if getattr(message, "animation", None):
        return GIF_NOT_SUPPORTED
    document = getattr(message, "document", None)
    mime = str(getattr(document, "mime_type", "") or "") if document else ""
    if mime.startswith("image/"):
        return SENT_AS_FILE.format(kind="عکس")
    if mime.startswith("video/"):
        return SENT_AS_FILE.format(kind="ویدیو")
    return ONLY_TEXT_OR_MEDIA


def has_content(message: Message) -> bool:
    """A text message, or a photo or video; stickers, files and voice notes are not group messages."""
    return bool(message.text) or media_from_message(message) is not None


def source_html(message: Message) -> str:
    """The text or caption as HTML when Telegram formatting is used, else as typed."""
    if message.text is not None:
        return (message.html_text if message.entities else message.text) or ""
    if message.caption is not None:
        return (message.html_text if message.caption_entities else message.caption) or ""
    return ""


def plain_source(message: Message) -> str:
    """The text or caption as typed (shop messages are sent exactly as written)."""
    return message.text or message.caption or ""


def media_data(media: BroadcastMedia | None) -> dict[str, str] | None:
    """FSM-storable form of ``media``."""
    return {"kind": media.kind, "file_id": media.file_id or ""} if media is not None else None


def media_from_data(data: dict) -> BroadcastMedia | None:
    raw = data.get(MEDIA_DATA_KEY)
    if not isinstance(raw, dict):
        return None
    return media_from_row(raw.get("kind"), raw.get("file_id"), None)


async def answer_preview(
    message: Message,
    body: str,
    media: BroadcastMedia | None,
    *,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    """Send ``body`` (with its media) in this chat exactly as the broadcast will."""
    extra: dict[str, object] = {"reply_markup": markup} if markup is not None else {}
    if parse_mode:
        extra["parse_mode"] = parse_mode
    if media is None:
        await message.answer(body, **extra)
        return
    caption = {"caption": body} if body else {}
    if media.kind == KIND_PHOTO:
        await message.answer_photo(media.file_id, **caption, **extra)
    else:
        await message.answer_video(media.file_id, **caption, **extra)
