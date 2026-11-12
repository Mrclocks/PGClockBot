"""Menu editor: rename and recolor the shop's own categories and plans.

The shop screens' edit panels («✏️ ویرایش این منو» on the shop and its plan lists) link here.
Callback data ``edr:<a|r>:<view>[:<id>[:<color>]]``: ``l`` the list, ``c``/``p`` one category or
plan, ``cn``/``pn`` ask for a new name, ``ci`` remove a category's icon, ``cc``/``pc`` set a color.
Every step re-checks edit mode and the catalog permission, and the services re-check that the row
belongs to the actor's shop.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.edit_mode import CATALOG_ROW_ROOT, EDIT_CALLBACK_ROOT, audience_code
from app.bot.handlers.plan_catalog_manage import resolve_catalog_staff
from app.bot.handlers.screen_editor import TEXT_EDIT_MODE_EXPIRED, TEXT_FEATURE_OFF, shop_editor_actor
from app.bot.reply_keyboard_state import is_menu_tap
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, Plan, PlanCategory
from app.services import bot_feedback_texts as feedback
from app.services import catalog_row_edits as rows
from app.services.button_color_editor import COLOR_NAMES, COLOR_SWATCHES
from app.services.plan_categories import split_category_label_from_message
from app.services.screen_editor import EditorActor, edit_mode_store, is_editor_enabled
from app.services.shop_scope import ShopScopeError
from app.services.user_errors import domain_error_text
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

owner_router = Router(name="catalog_row_editor_owner")
shop_router = Router(name="catalog_row_editor_shop")

_REQUEST = re.compile(r"^edr:(a|r):(l|c|p|cn|pn|ci|cc|pc)(?::(\d{1,10}))?(?::([wpsdi]))?$")
_NEEDS_ID = frozenset({"c", "p", "cn", "pn", "ci", "cc", "pc"})
_NEEDS_COLOR = frozenset({"cc", "pc"})

TITLE = "🗂 دسته‌ها و پلن‌های فروشگاه"
BTN_RENAME_CATEGORY = "✏️ نام و ایموجی"
BTN_RENAME_PLAN = "✏️ نام"
BTN_CLEAR_ICON = "🧹 حذف ایموجی"
BTN_BACK = "⬅️ بازگشت"
INHERIT_LABEL = {"c": "↺ رنگ نوع پلن", "p": "↺ رنگ دسته"}
TEXT_SAVED = "✅ ذخیره شد."
TEXT_EMPTY = "هنوز دسته یا پلنی برای این فروشگاه ساخته نشده است."
PROMPT_CATEGORY = "نام تازهٔ دسته را بفرستید. اگر یک ایموجی پرمیوم هم کنارش بفرستید، آیکون دسته می‌شود."
PROMPT_PLAN = "نام تازهٔ پلن را بفرستید."


class CatalogRowStates(StatesGroup):
    owner_name = State()
    shop_name = State()


@dataclass(frozen=True, slots=True)
class RowRequest:
    view: str
    row_id: int = 0
    color: str = ""


def parse_request(data: str | None) -> RowRequest | None:
    match = _REQUEST.match(data or "")
    if match is None:
        return None
    _, view, row_id, color = match.groups()
    if (view in _NEEDS_ID) != (row_id is not None) or (view in _NEEDS_COLOR) != (color is not None):
        return None
    return RowRequest(view, int(row_id or 0), color or "")


def _cb(actor: EditorActor, *parts: object) -> str:
    return ":".join([CATALOG_ROW_ROOT, audience_code(actor), *map(str, parts)])


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=data)


def _swatch(color: str | None, *, inherit: str) -> str:
    if color is None:
        return inherit
    return f"{COLOR_SWATCHES.get(color, '⚪')} {COLOR_NAMES.get(color, color)}"


def _color_rows(actor: EditorActor, kind: str, row_id: int, current: str | None) -> list[list[InlineKeyboardButton]]:
    def label(code: str) -> str:
        color = rows.COLOR_CODES[code]
        text = INHERIT_LABEL[kind] if color is None else _swatch(color, inherit="")
        return f"✓ {text}" if color == current else text

    swatches = [_button(label(code), _cb(actor, f"{kind}c", row_id, code)) for code in ("w", "p", "s", "d")]
    return [swatches[:2], swatches[2:], [_button(label("i"), _cb(actor, f"{kind}c", row_id, "i"))]]


def list_view(actor: EditorActor, catalog: rows.CatalogRows) -> tuple[str, InlineKeyboardMarkup]:
    keyboard = [[_button(f"🏷 {c.name}", _cb(actor, "c", c.id))] for c in catalog.categories]
    keyboard += [[_button(f"💎 {p.name}", _cb(actor, "p", p.id))] for p in catalog.plans]
    keyboard.append([_button(BTN_BACK, f"{EDIT_CALLBACK_ROOT}:{audience_code(actor)}:o:c.shop")])
    hint = "یک دسته یا پلن را برای تغییر نام یا رنگ انتخاب کنید." if len(keyboard) > 1 else TEXT_EMPTY
    return f"<b>{TITLE}</b>\n{hint}", InlineKeyboardMarkup(inline_keyboard=keyboard)


def category_view(actor: EditorActor, category: PlanCategory) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"🏷 <b>{html.escape(category.name)}</b>",
        f"ایموجی: {'دارد' if category.icon_custom_emoji_id else 'ندارد'}",
        f"رنگ: {_swatch(category.button_style, inherit=INHERIT_LABEL['c'])}",
    ]
    keyboard = [[_button(BTN_RENAME_CATEGORY, _cb(actor, "cn", category.id))]]
    if category.icon_custom_emoji_id:
        keyboard.append([_button(BTN_CLEAR_ICON, _cb(actor, "ci", category.id))])
    keyboard += _color_rows(actor, "c", category.id, category.button_style)
    keyboard.append([_button(BTN_BACK, _cb(actor, "l"))])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


def plan_view(actor: EditorActor, plan: Plan) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"💎 <b>{html.escape(plan.name)}</b>", f"رنگ: {_swatch(plan.button_style, inherit=INHERIT_LABEL['p'])}"]
    keyboard = [[_button(BTN_RENAME_PLAN, _cb(actor, "pn", plan.id))]]
    keyboard += _color_rows(actor, "p", plan.id, plan.button_style)
    keyboard.append([_button(BTN_BACK, _cb(actor, "l"))])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


@dataclass(slots=True)
class _Turn:
    session: AsyncSession
    actor: EditorActor
    staff: dict
    state: FSMContext


View = tuple[str, InlineKeyboardMarkup]


async def _show_list(turn: _Turn, request: RowRequest) -> View:
    return list_view(turn.actor, await rows.editable_rows(turn.session, turn.staff))


async def _show_category(turn: _Turn, request: RowRequest) -> View:
    return category_view(turn.actor, await rows.owned_category(turn.session, turn.staff, request.row_id))


async def _show_plan(turn: _Turn, request: RowRequest) -> View:
    return plan_view(turn.actor, await rows.owned_plan(turn.session, turn.staff, request.row_id))


async def _clear_icon(turn: _Turn, request: RowRequest) -> View:
    return category_view(turn.actor, await rows.clear_category_icon(turn.session, turn.staff, request.row_id))


async def _color_category(turn: _Turn, request: RowRequest) -> View:
    category = await rows.set_category_color(turn.session, turn.staff, request.row_id, request.color)
    return category_view(turn.actor, category)


async def _color_plan(turn: _Turn, request: RowRequest) -> View:
    return plan_view(turn.actor, await rows.set_plan_color(turn.session, turn.staff, request.row_id, request.color))


async def _ask_name(turn: _Turn, request: RowRequest) -> View:
    kind = request.view[0]
    owner = rows.owned_category if kind == "c" else rows.owned_plan
    await owner(turn.session, turn.staff, request.row_id)
    name_state = CatalogRowStates.shop_name if turn.actor.for_reseller else CatalogRowStates.owner_name
    await turn.state.set_state(name_state)
    await turn.state.update_data(edr_kind=kind, edr_id=request.row_id)
    back = _cb(turn.actor, kind, request.row_id)
    prompt = PROMPT_CATEGORY if kind == "c" else PROMPT_PLAN
    return prompt, InlineKeyboardMarkup(inline_keyboard=[[_button(f"❌ {kb.BTN_CANCEL}", back)]])


_VIEWS: dict[str, Callable[[_Turn, RowRequest], Awaitable[View]]] = {
    "l": _show_list,
    "c": _show_category,
    "p": _show_plan,
    "ci": _clear_icon,
    "cc": _color_category,
    "pc": _color_plan,
    "cn": _ask_name,
    "pn": _ask_name,
}
_CHANGES = frozenset({"ci", "cc", "pc"})


async def _editing_staff(
    session: AsyncSession, db_user: BotUser, actor: EditorActor, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> dict:
    if not await is_editor_enabled(session):
        raise ValueError(TEXT_FEATURE_OFF)
    if not edit_mode_store.is_active(actor):
        raise ValueError(TEXT_EDIT_MODE_EXPIRED)
    staff = await resolve_catalog_staff(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if staff is None:
        raise ShopScopeError(feedback.NO_ACCESS)
    return staff


async def _handle(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext, actor: EditorActor,
    *, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> None:
    request = parse_request(callback.data)
    if request is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        staff = await _editing_staff(
            session, db_user, actor, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
        )
        if request.view not in {"cn", "pn"}:
            await state.set_state(None)
        text, markup = await _VIEWS[request.view](_Turn(session, actor, staff, state), request)
    except (ValueError, ShopScopeError) as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    if request.view in _CHANGES:
        edit_mode_store.extend(actor)
    await callback.answer(TEXT_SAVED if request.view in _CHANGES else None)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


async def _save_name(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser, actor: EditorActor,
    *, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> None:
    data = await state.get_data()
    kind, row_id = data.get("edr_kind"), int(data.get("edr_id") or 0)
    if is_menu_tap(message, await get_all_settings(session, reseller_id=actor.settings_scope)):
        # The person left the editor by tapping a menu button: stop waiting and let navigation run.
        await state.set_state(None)
        raise SkipHandler()
    cancelled = kb.is_cancel_text((message.text or "").strip())
    try:
        staff = await _editing_staff(
            session, db_user, actor, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
        )
        turn = _Turn(session, actor, staff, state)
        if not cancelled:
            await _rename(turn, kind, row_id, message)
            edit_mode_store.extend(actor)
        view = await _VIEWS["c" if kind == "c" else "p"](turn, RowRequest(str(kind), row_id))
    except (ValueError, ShopScopeError) as exc:
        await message.answer(domain_error_text(exc))
        return
    await state.set_state(None)
    text, markup = view
    await message.answer(f"{feedback.CANCELLED if cancelled else TEXT_SAVED}\n\n{text}", reply_markup=markup)


async def _rename(turn: _Turn, kind: str | None, row_id: int, message: Message) -> None:
    if kind == "c":
        name, icon = split_category_label_from_message(message.text, message.entities)
        await rows.rename_category(turn.session, turn.staff, row_id, name=name, icon_custom_emoji_id=icon)
    elif kind == "p":
        await rows.rename_plan(turn.session, turn.staff, row_id, name=message.text or "")
    else:
        raise ValueError(feedback.INVALID)


# ── Routers ──────────────────────────────────────────────────────────────────


@owner_router.callback_query(F.data.startswith("edr:a:"))
async def owner_rows(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    actor = EditorActor(user_id=int(db_user.id), shop_id=None)
    await _handle(callback, session, db_user, state, actor, is_reseller_bot=False, reseller_owner_id=None)


@owner_router.message(CatalogRowStates.owner_name)
async def owner_row_name(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser) -> None:
    actor = EditorActor(user_id=int(db_user.id), shop_id=None)
    await _save_name(message, state, session, db_user, actor, is_reseller_bot=False, reseller_owner_id=None)


@shop_router.callback_query(F.data.startswith("edr:r:"))
async def shop_rows(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await shop_editor_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_SHOP_SETTINGS_ACCESS, show_alert=True)
        return
    await _handle(
        callback, session, db_user, state, actor, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )


@shop_router.message(CatalogRowStates.shop_name)
async def shop_row_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await shop_editor_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await state.set_state(None)
        await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
        return
    await _save_name(
        message, state, session, db_user, actor, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
