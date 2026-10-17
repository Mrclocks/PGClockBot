"""Connection guides — inline list + detail for users and resellers."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import delete_user_message
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, UserService
from app.services.connection_guides import (
    get_connection_guides,
    guide_import_url,
    guides_for_audience,
)
from app.services.formatting import format_message
from app.services.guide_media import KIND_VIDEO, MediaToSend, load_file_id_cache, media_to_send, remember_file_ids
from app.services.service_scope import customer_may_use_service
from app.services.users import current_shop_reseller_id, get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="guides")


def _item_style(ui: dict, item_style: str | None = None) -> str | None:
    from app.services.button_styles import normalize_style, style_or_none

    if item_style is not None and str(item_style).strip() != "":
        s = normalize_style(item_style)
        return s or None
    return style_or_none(ui, "guides", fallback="primary")


def guides_list_keyboard(
    items: list[dict],
    ui: dict,
    *,
    svc_id: int | None = None,
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = []
    prefix = f"guide:open:{svc_id}:" if svc_id else "guide:open:0:"
    for g in items[: kb.LIST_BUTTON_CAP]:
        gid = str(g.get("id") or "")
        title = str(g.get("title") or "آموزش")[:64]
        kwargs: dict = {
            "text": title,
            "callback_data": f"{prefix}{gid}"[:64],
        }
        st = _item_style(ui, g.get("style") or g.get("button_style"))
        if st:
            kwargs["style"] = st
        buttons.append(InlineKeyboardButton(**kwargs))
    rows = kb.chunk_buttons(buttons, cols=2)
    if back_callback:
        rows.append([kb.back_button(ui, back_callback)])
    else:
        rows.append([InlineKeyboardButton(text="🏠 منو", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def guide_detail_keyboard(
    guide: dict,
    ui: dict,
    *,
    svc_id: int | None = None,
    sub_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if guide.get("deep_link"):
        url = guide_import_url(sub_url)
        if url:
            label = kb._label_or(ui, "btn_guide_open_link", "🔗 باز کردن لینک اشتراک").strip()[:64]
            rows.append([kb._ikb(label, url=url, ui=ui, label_key="btn_guide_open_link")])
    rows.extend([InlineKeyboardButton(text=link["label"][:64], url=link["url"])] for link in guide.get("links") or [])
    back = f"guide:list:{int(svc_id or 0)}"
    rows.append([kb.back_button(ui, back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_guides_keyboard(ui: dict, *, audience: str = "user") -> InlineKeyboardMarkup:
    """Single entry button attached to delivery messages."""
    label = (kb._t(ui, "btn_guides") or "📘 آموزش اتصال").strip()[:64]
    kwargs: dict = {
        "text": label,
        "callback_data": f"guide:list:0:{audience}"[:64],
    }
    st = _item_style(ui, None)
    if st:
        kwargs["style"] = st
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**kwargs)]])


async def _audience_for_user(session: AsyncSession, db_user: BotUser) -> str:
    role = (getattr(db_user, "role", None) or "").lower()
    if role == "reseller":
        return "reseller"
    return "user"


async def show_guides_list(
    target: Message | CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    *,
    svc_id: int | None = None,
    audience: str | None = None,
) -> None:
    ui = await get_all_settings(session)
    aud = audience or await _audience_for_user(session, db_user)
    items = guides_for_audience(await get_connection_guides(session), aud)
    text = format_message(
        "📘 آموزش اتصال",
        ("یک آموزش را انتخاب کنید:" + kb.list_overflow_line(len(items)))
        if items
        else "هنوز آموزشی تعریف نشده است.",
    )
    markup = guides_list_keyboard(items, ui, svc_id=svc_id) if items else kb.back_home(ui)
    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("guide:list:"))
async def guide_list_cb(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    parts = (callback.data or "").split(":")
    # guide:list:{svc_id}[:audience]
    svc_id = 0
    audience = None
    try:
        svc_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        svc_id = 0
    if len(parts) > 3 and parts[3] in {"user", "reseller"}:
        audience = parts[3]
    await show_guides_list(
        callback,
        session,
        db_user,
        svc_id=svc_id or None,
        audience=audience,
    )


@router.callback_query(F.data.startswith("guide:open:"))
async def guide_open_cb(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    parts = (callback.data or "").split(":")
    # guide:open:{svc_id}:{guide_id}
    svc_id = 0
    gid = ""
    try:
        svc_id = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        svc_id = 0
    if len(parts) > 3:
        gid = ":".join(parts[3:])
    ui = await get_all_settings(session)
    items = await get_connection_guides(session)
    guide = next((g for g in items if str(g.get("id")) == gid and g.get("enabled", True)), None)
    if not guide:
        await callback.answer("آموزش یافت نشد", show_alert=True)
        return
    sub_url = None
    if svc_id:
        svc = await session.get(UserService, svc_id)
        if await customer_may_use_service(session, svc, db_user.id, current_shop_reseller_id()):
            sub_url = svc.subscription_url
    body = (guide.get("body") or "").strip() or "—"
    title = str(guide.get("title") or "آموزش")
    await callback.answer()
    if not callback.message:
        return
    text = format_message(f"📘 {title}", body)
    markup = guide_detail_keyboard(guide, ui, svc_id=svc_id, sub_url=sub_url)
    rid = current_shop_reseller_id()
    cache = await load_file_id_cache(session, reseller_id=rid)
    album = media_to_send(guide, bot_id=callback.bot.id, cache=cache, reseller_id=rid)
    if not album:
        await safe_edit_text(callback.message, text, reply_markup=markup)
        return
    learned = await send_guide_album(callback.bot, callback.message.chat.id, album)
    await callback.message.answer(text, reply_markup=markup)
    await delete_user_message(callback.message)
    if learned:
        live = {m["id"] for g in items for m in g.get("media") or []}
        await remember_file_ids(session, learned, bot_id=callback.bot.id, live_media_ids=live, reseller_id=rid)


def _album_item(item: MediaToSend) -> InputMediaPhoto | InputMediaVideo:
    source = item.file_id or FSInputFile(item.path)
    return InputMediaVideo(media=source) if item.kind == KIND_VIDEO else InputMediaPhoto(media=source)


def _sent_file_id(message: Message) -> str | None:
    if message.video:
        return message.video.file_id
    return message.photo[-1].file_id if message.photo else None


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

