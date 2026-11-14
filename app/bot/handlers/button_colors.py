"""«🎨 رنگ دکمه‌ها» in the bot settings: browse catalog groups and pick a color.

``owner_router`` serves the platform owner and is registered behind the
owner-principal middleware; ``shop_router`` serves a shop owner for their own
shop. Rules live in ``app.services.button_color_editor``; these handlers only
parse callbacks, check the actor, call it and render.

Callback data: ``bcl:<a|r>:g`` groups, ``bcl:<a|r>:g:<group>:<page>`` one
group, ``bcl:<a|r>:i:<group>:<style_id>`` picker,
``bcl:<a|r>:s:<group>:<style_id>:<w|p|s|d|x>`` save (``x`` = default).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.user_errors import domain_error_text
from app.services import bot_feedback_texts as feedback
from app.services.button_color_editor import (
    COLOR_NAMES,
    COLOR_SWATCHES,
    PLATFORM_SCOPE,
    ColorGroup,
    ColorItem,
    color_groups,
    find_color_item,
    reset_button_color,
    set_button_color,
    shared_button_count,
)
from app.services.resellers import has_bot_perm
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

owner_router = Router(name="button_colors_owner")
shop_router = Router(name="button_colors_shop")

ITEMS_PER_PAGE = 8
# Settings section (same id in the owner and shop settings) that links here.
COLORS_SECTION = "menu"
_CALLBACK_ROOT = "bcl"
_CODE_TO_COLOR = {"w": "", "p": "primary", "s": "success", "d": "danger"}
_COLOR_TO_CODE = {color: code for code, color in _CODE_TO_COLOR.items()}
_RESET_CODE = "x"
_SETTINGS_BACK = {"a": "adm:st:sec:menu", "r": "res:st:sec:menu"}

TITLE_GROUPS = "🎨 <b>رنگ دکمه‌ها</b>\nیک گروه را انتخاب کنید."
HINT_GROUP = "روی دکمه‌ای بزنید تا رنگش را عوض کنید."
SHARED_WARNING = "⚠️ این رنگ بین {count} دکمه مشترک است و همه با هم عوض می‌شوند."
BTN_ENTRY = "🎨 رنگ دکمه‌ها"
BTN_BACK = "⬅️ بازگشت"
BTN_RESET = "↺ پیش‌فرض"
BTN_PREV = "◀️ قبل"
BTN_NEXT = "بعد ▶️"


@dataclass(frozen=True, slots=True)
class ColorRequest:
    """A parsed callback: which screen, group, page, button and color code."""

    audience: str
    view: str
    group_index: int = 0
    page: int = 0
    style_id: str = ""
    code: str = ""


def entry_button(*, for_reseller: bool) -> InlineKeyboardButton:
    """Button placed in the «ظاهر و دکمه‌ها» / «منو» settings section."""
    audience = "r" if for_reseller else "a"
    return InlineKeyboardButton(text=BTN_ENTRY, callback_data=f"{_CALLBACK_ROOT}:{audience}:g")


def parse_request(data: str | None) -> ColorRequest | None:
    """Parse callback data; None for anything malformed."""
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != _CALLBACK_ROOT or parts[1] not in _SETTINGS_BACK:
        return None
    audience, view = parts[1], parts[2]
    try:
        if view == "g" and len(parts) == 3:
            return ColorRequest(audience, "groups")
        if view == "g" and len(parts) == 5:
            return ColorRequest(audience, "group", group_index=int(parts[3]), page=int(parts[4]))
        if view == "i" and len(parts) == 5:
            return ColorRequest(audience, "item", group_index=int(parts[3]), style_id=parts[4])
        if view == "s" and len(parts) == 6:
            return ColorRequest(
                audience, "save", group_index=int(parts[3]), style_id=parts[4], code=parts[5]
            )
    except ValueError:
        return None
    return None


def _button(text: str, data: str, *, style: str | None = None) -> InlineKeyboardButton:
    if style:
        return InlineKeyboardButton(text=text, callback_data=data, style=style)
    return InlineKeyboardButton(text=text, callback_data=data)


def _swatch(color: str) -> str:
    return f"{COLOR_SWATCHES.get(color, '⚪')} {COLOR_NAMES.get(color, color)}"


def groups_view(groups: list[ColorGroup], audience: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = [
        _button(group.name, f"{_CALLBACK_ROOT}:{audience}:g:{index}:0")
        for index, group in enumerate(groups)
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_button(BTN_BACK, _SETTINGS_BACK[audience])])
    return TITLE_GROUPS, InlineKeyboardMarkup(inline_keyboard=rows)


def group_view(
    group: ColorGroup, audience: str, group_index: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, -(-len(group.items) // ITEMS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    shown = group.items[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]
    rows = [
        [
            _button(
                f"{COLOR_SWATCHES.get(item.color, '⚪')} {item.label}",
                f"{_CALLBACK_ROOT}:{audience}:i:{group_index}:{item.style_id}",
            )
        ]
        for item in shown
    ]
    pager = []
    if page > 0:
        pager.append(_button(BTN_PREV, f"{_CALLBACK_ROOT}:{audience}:g:{group_index}:{page - 1}"))
    if page < pages - 1:
        pager.append(_button(BTN_NEXT, f"{_CALLBACK_ROOT}:{audience}:g:{group_index}:{page + 1}"))
    if pager:
        rows.append(pager)
    rows.append([_button(BTN_BACK, f"{_CALLBACK_ROOT}:{audience}:g")])
    title = f"🎨 <b>{html.escape(group.name)}</b>"
    if pages > 1:
        title += f" ({page + 1}/{pages})"
    return f"{title}\n{HINT_GROUP}", InlineKeyboardMarkup(inline_keyboard=rows)


def picker_view(
    item: ColorItem, audience: str, group_index: int, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    prefix = f"{_CALLBACK_ROOT}:{audience}:s:{group_index}:{item.style_id}"
    color_buttons = [
        _button(
            f"{'✓ ' if color == item.color else ''}{_swatch(color)}",
            f"{prefix}:{code}",
            style=color or None,
        )
        for color, code in _COLOR_TO_CODE.items()
    ]
    rows = [color_buttons[0:2], color_buttons[2:4]]
    rows.append([_button(BTN_RESET, f"{prefix}:{_RESET_CODE}")])
    rows.append([_button(BTN_BACK, f"{_CALLBACK_ROOT}:{audience}:g:{group_index}:{page}")])
    lines = [
        f"🎨 <b>{html.escape(item.label)}</b>",
        f"رنگ فعلی: {_swatch(item.color)}",
        f"پیش‌فرض: {_swatch(item.default)}",
    ]
    shared = shared_button_count(item.style_id)
    if shared > 1:
        lines.extend(["", SHARED_WARNING.format(count=shared)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@dataclass(frozen=True, slots=True)
class _Actor:
    for_reseller: bool
    shop_id: int | None


async def shop_settings_shop_id(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> int | None:
    """The shop whose settings this person may change (shop_settings permission), else None."""
    from app.services.reseller_access import load_reseller_actor

    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile or not has_bot_perm(profile, "shop_settings"):
        return None
    return int(profile.user_id)


async def _shop_actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> _Actor | None:
    shop_id = await shop_settings_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return _Actor(for_reseller=True, shop_id=shop_id) if shop_id else None


async def _handle(callback: CallbackQuery, session: AsyncSession, actor: _Actor, request: ColorRequest) -> None:
    scope = actor.shop_id if actor.for_reseller else PLATFORM_SCOPE
    ui = await get_all_settings(session, reseller_id=scope)
    groups = color_groups(ui, for_reseller=actor.for_reseller)
    if request.view == "groups":
        await callback.answer()
        await _show(callback, groups_view(groups, request.audience))
        return
    if not 0 <= request.group_index < len(groups):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    group = groups[request.group_index]
    if request.view == "group":
        await callback.answer()
        await _show(callback, group_view(group, request.audience, request.group_index, request.page))
        return
    position = next((i for i, it in enumerate(group.items) if it.style_id == request.style_id), None)
    if position is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        item = find_color_item(ui, request.style_id, for_reseller=actor.for_reseller)
        if request.view == "save":
            item = await _save(session, actor, item, request.code)
            await callback.answer(feedback.COLOR_SAVED)
        else:
            await callback.answer()
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    page = position // ITEMS_PER_PAGE
    await _show(callback, picker_view(item, request.audience, request.group_index, page))


async def _save(session: AsyncSession, actor: _Actor, item: ColorItem, code: str) -> ColorItem:
    if code == _RESET_CODE:
        color = await reset_button_color(
            session, style_id=item.style_id, for_reseller=actor.for_reseller, shop_id=actor.shop_id
        )
    elif code in _CODE_TO_COLOR:
        color = await set_button_color(
            session,
            style_id=item.style_id,
            color=_CODE_TO_COLOR[code],
            for_reseller=actor.for_reseller,
            shop_id=actor.shop_id,
        )
    else:
        raise ValueError(feedback.INVALID)
    return ColorItem(style_id=item.style_id, label=item.label, color=color, default=item.default)


async def _show(callback: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    text, markup = view
    await safe_edit_text(callback.message, text, reply_markup=markup)


# Literal prefixes (_CALLBACK_ROOT) keep the handlers visible to the callback contract check.
@owner_router.callback_query(F.data.startswith("bcl:a:"))
async def owner_colors(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    request = parse_request(callback.data)
    if request is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await _handle(callback, session, _Actor(for_reseller=False, shop_id=None), request)


@shop_router.callback_query(F.data.startswith("bcl:r:"))
async def shop_colors(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    request = parse_request(callback.data)
    if request is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    actor = await _shop_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if actor is None:
        await callback.answer(feedback.NO_SHOP_SETTINGS_ACCESS, show_alert=True)
        return
    await _handle(callback, session, actor, request)
