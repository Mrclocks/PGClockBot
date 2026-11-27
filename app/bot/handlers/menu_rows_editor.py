"""Menu editor: «▦ چیدمان منو», how many buttons share each row of one menu.

Opened from a menu's edit panel («✏️ ویرایش این منو»); «⬅️ بازگشت» returns there.
Callback data ``edl:<a|r>:<view>:<screen>[:<sizes>[:<row>]]``: ``o`` open with the saved
rows, ``p``/``m`` one more / one fewer button in row ``row`` of the draft ``sizes``,
``s`` save the draft, ``r`` back to the default rows, ``i`` a hint for the row labels.
The draft travels in the callback (one digit per row), so nothing is saved until
«✅ ذخیره». Every step rebuilds the real menu, re-checks edit mode and the
actor's audience, and refuses a draft that no longer fits the menu's buttons.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.edit_mode import EDIT_CALLBACK_ROOT, MENU_ROWS_ROOT, audience_code
from app.bot.handlers.screen_editor import (
    TEXT_EDIT_MODE_EXPIRED,
    TEXT_FEATURE_OFF,
    TEXT_SHOP_BOT_ONLY,
    shop_editor_actor,
)
from app.bot.row_layout import MenuBody
from app.bot.screen_registry import SCREENS, ScreenContext, ScreenSpec, screen_body
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.menu_rows import (
    TEXT_NOT_EDITABLE,
    decode_sizes,
    encode_sizes,
    narrow_row,
    reset_menu_rows,
    save_menu_rows,
    split_rows,
    widen_row,
)
from app.services.numbers import to_fa_digits
from app.services.resellers import get_reseller_profile
from app.services.screen_editor import EditorActor, edit_mode_store, is_editor_enabled
from app.services.user_errors import domain_error_text
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

owner_router = Router(name="menu_rows_editor_owner")
shop_router = Router(name="menu_rows_editor_shop")

_REQUEST = re.compile(r"^edl:(a|r):(o|p|m|s|r|i):([a-z][a-z0-9._]{0,23})(?::([1-4]{1,24}))?(?::(\d{1,2}))?$")
_NEEDS_SIZES = frozenset({"p", "m", "s"})
_NEEDS_ROW = frozenset({"p", "m"})

BTN_WIDEN = "➕"
BTN_NARROW = "➖"
BTN_SAVE = "✅ ذخیره"
BTN_RESET = "↺ پیش‌فرض"
BTN_BACK = "⬅️ بازگشت"
TEXT_HINT = "با ➖ و ➕ تعداد ستون هر ردیف را کم و زیاد کنید؛ ترتیب دکمه‌ها همان می‌ماند."
TEXT_ROW_HINT = "برای تغییر تعداد ستون این ردیف، ➖ یا ➕ کنارش را بزنید."
TEXT_UNSAVED = "⚠️ این چیدمان هنوز ذخیره نشده."
TEXT_SAVED = "✅ چیدمان منو ذخیره شد."
TEXT_RESET = "✅ چیدمان به حالت پیش‌فرض برگشت."
BUTTON_TEXT_PREVIEW = 24


@dataclass(frozen=True, slots=True)
class RowsRequest:
    audience: str
    view: str
    screen_id: str
    sizes: str = ""
    row: int = -1


def parse_request(data: str | None) -> RowsRequest | None:
    match = _REQUEST.match(data or "")
    if match is None:
        return None
    audience, view, screen_id, sizes, row = match.groups()
    if screen_id not in SCREENS:
        return None
    if (view in _NEEDS_SIZES) != (sizes is not None) or (view in _NEEDS_ROW) != (row is not None):
        return None
    return RowsRequest(audience, view, screen_id, sizes or "", int(row) if row is not None else -1)


def _cb(actor: EditorActor, *parts: object) -> str:
    return ":".join([MENU_ROWS_ROOT, audience_code(actor), *map(str, parts)])


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _sizes_of(rows: tuple[tuple[str, ...], ...]) -> list[int]:
    return [len(row) for row in rows]


# ── View ─────────────────────────────────────────────────────────────────────


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= BUTTON_TEXT_PREVIEW else text[: BUTTON_TEXT_PREVIEW - 1] + "…"


def rows_view(
    actor: EditorActor, spec: ScreenSpec, body: MenuBody, draft: list[int]
) -> tuple[str, InlineKeyboardMarkup]:
    sizes = encode_sizes(draft)
    lines = [f"▦ <b>چیدمان منو: {html.escape(spec.title)}</b>", TEXT_HINT, ""]
    keyboard: list[list[InlineKeyboardButton]] = []
    start = 0
    for index, size in enumerate(draft):
        texts = " | ".join(html.escape(_short(text)) for text in body.texts[start : start + size])
        lines.append(f"ردیف {to_fa_digits(index + 1)}: {texts}")
        keyboard.append([
            _button(BTN_NARROW, _cb(actor, "m", spec.id, sizes, index)),
            _button(f"ردیف {to_fa_digits(index + 1)} · {to_fa_digits(size)} ستون", _cb(actor, "i", spec.id)),
            _button(BTN_WIDEN, _cb(actor, "p", spec.id, sizes, index)),
        ])
        start += size
    if draft != _sizes_of(body.rows):
        lines.extend(["", TEXT_UNSAVED])
    keyboard.append([_button(BTN_SAVE, _cb(actor, "s", spec.id, sizes)), _button(BTN_RESET, _cb(actor, "r", spec.id))])
    keyboard.append([_button(BTN_BACK, f"{EDIT_CALLBACK_ROOT}:{audience_code(actor)}:o:{spec.id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


# ── Steps ────────────────────────────────────────────────────────────────────


async def _load_body(session: AsyncSession, actor: EditorActor, screen_id: str) -> tuple[ScreenSpec, MenuBody]:
    spec = SCREENS.get(screen_id)
    if spec is None or spec.audience.value not in actor.audiences:
        raise ValueError(feedback.INVALID)
    ui = await get_all_settings(session, reseller_id=actor.settings_scope)
    profile = await get_reseller_profile(session, actor.shop_id) if actor.for_reseller else None
    body = screen_body(screen_id, ScreenContext(ui=ui, profile=profile))
    if body is None:
        raise ValueError(TEXT_NOT_EDITABLE)
    return spec, body


async def _check_editing(session: AsyncSession, actor: EditorActor) -> None:
    if not await is_editor_enabled(session):
        raise ValueError(TEXT_FEATURE_OFF)
    if not edit_mode_store.is_active(actor):
        raise ValueError(TEXT_EDIT_MODE_EXPIRED)


async def _step(
    session: AsyncSession, actor: EditorActor, request: RowsRequest
) -> tuple[str | None, tuple[str, InlineKeyboardMarkup]]:
    """Run one step; return the toast and the view to show."""
    await _check_editing(session, actor)
    spec, body = await _load_body(session, actor, request.screen_id)
    if request.view == "o":
        return None, rows_view(actor, spec, body, _sizes_of(body.rows))
    if request.view in _NEEDS_ROW:
        change = widen_row if request.view == "p" else narrow_row
        draft = change(decode_sizes(request.sizes, total=len(body.keys)), request.row)
        return None, rows_view(actor, spec, body, draft)
    if request.view == "s":
        rows = split_rows(body.keys, decode_sizes(request.sizes, total=len(body.keys)))
        await save_menu_rows(
            session, actor, screen_id=spec.id, audience=spec.audience.value,
            rows=rows, default_rows=body.default_rows,
        )
        toast = TEXT_SAVED
    else:
        await reset_menu_rows(session, actor, screen_id=spec.id, audience=spec.audience.value)
        toast = TEXT_RESET
    edit_mode_store.extend(actor)
    spec, body = await _load_body(session, actor, spec.id)
    return toast, rows_view(actor, spec, body, _sizes_of(body.rows))


async def _handle(callback: CallbackQuery, session: AsyncSession, actor: EditorActor, request: RowsRequest) -> None:
    if request.view == "i":
        await callback.answer(TEXT_ROW_HINT)
        return
    try:
        toast, (text, markup) = await _step(session, actor, request)
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer(toast)
    await safe_edit_text(callback.message, text, reply_markup=markup)


# ── Routers ──────────────────────────────────────────────────────────────────


# Literal prefixes (MENU_ROWS_ROOT) keep the handlers visible to the callback contract check.
@owner_router.callback_query(F.data.startswith("edl:a:"))
async def owner_rows(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    request = parse_request(callback.data)
    if request is None or request.audience != "a":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await _handle(callback, session, EditorActor(user_id=int(db_user.id), shop_id=None), request)


@shop_router.callback_query(F.data.startswith("edl:r:"))
async def shop_rows(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    request = parse_request(callback.data)
    if request is None or request.audience != "r":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    actor = await shop_editor_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        text = TEXT_SHOP_BOT_ONLY if not is_reseller_bot else feedback.NO_SHOP_SETTINGS_ACCESS
        await callback.answer(text, show_alert=True)
        return
    await _handle(callback, session, actor, request)
