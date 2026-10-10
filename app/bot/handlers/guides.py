"""Connection guides — inline list + detail for users and resellers."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, UserService
from app.services.connection_guides import (
    get_connection_guides,
    guide_import_url,
    guides_for_audience,
)
from app.services.formatting import format_message
from app.services.users import get_all_settings

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
    rows: list[list[InlineKeyboardButton]] = []
    prefix = f"guide:open:{svc_id}:" if svc_id else "guide:open:0:"
    for g in items:
        gid = str(g.get("id") or "")
        title = str(g.get("title") or "آموزش")[:64]
        kwargs: dict = {
            "text": title,
            "callback_data": f"{prefix}{gid}"[:64],
        }
        st = _item_style(ui, g.get("style") or g.get("button_style"))
        if st:
            kwargs["style"] = st
        rows.append([InlineKeyboardButton(**kwargs)])
    if back_callback:
        rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_callback)])
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
            label = (ui.get("btn_guide_open_link") or "🔗 باز کردن لینک اشتراک").strip()[:64]
            rows.append([InlineKeyboardButton(text=label, url=url)])
    back = f"guide:list:{int(svc_id or 0)}"
    rows.append([InlineKeyboardButton(text="⬅️ فهرست آموزش", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_guides_keyboard(ui: dict, *, audience: str = "user") -> InlineKeyboardMarkup:
    """Single entry button attached to delivery messages."""
    label = (ui.get("btn_guides") or "📘 آموزش اتصال").strip()[:64]
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
        "یک آموزش را انتخاب کنید:" if items else "هنوز آموزشی تعریف نشده است.",
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
        if svc and svc.bot_user_id == db_user.id:
            sub_url = svc.subscription_url
    body = (guide.get("body") or "").strip() or "—"
    title = str(guide.get("title") or "آموزش")
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(f"📘 {title}", body),
            reply_markup=guide_detail_keyboard(guide, ui, svc_id=svc_id, sub_url=sub_url),
        )

