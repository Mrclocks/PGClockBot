"""In-menu editor: switch edit mode, list a menu's buttons, change text or color.

``owner_router`` (platform owner, behind the owner-principal middleware) and
``shop_router`` (shop owner, own shop only, shop bot only) share the views.
Callback data ``ed:<a|r>:<view>[:<screen>[:<button>[:<color>]]]``:
``h`` switches, ``f1``/``f0`` feature on/off (owner), ``m1``/``m0`` my edit
mode, ``o`` a menu's buttons, ``b`` one button, ``t`` new text, ``c`` color,
``n``/``nr`` new/default title, ``d``/``dh``/``dr`` new/hidden/default caption;
every view but the switches needs my edit mode to be on.
Button numbers are positions in ``screen_buttons``; every step rebuilds the
real menu and re-checks the position and permissions before acting.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.edit_mode import EDIT_CALLBACK_ROOT, audience_code
from app.bot.handlers.button_colors import shop_settings_shop_id
from app.bot.reply_keyboard_state import is_menu_tap
from app.bot.screen_registry import SCREENS, ScreenButton, ScreenContext, ScreenSpec, screen_buttons
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.user_errors import domain_error_text
from app.services import bot_feedback_texts as feedback
from app.services.button_color_editor import (
    COLOR_NAMES,
    COLOR_SWATCHES,
    ColorItem,
    find_color_item,
    reset_button_color,
    set_button_color,
    shared_button_count,
)
from app.services.resellers import get_reseller_profile
from app.services.rich_text import pack_setting_from_message
from app.services.screen_editor import (
    EDIT_MODE_MINUTES,
    LABEL_MAX_LENGTH,
    EditorActor,
    can_edit_label,
    edit_mode_store,
    is_editor_enabled,
    hide_screen_caption,
    reset_screen_text,
    save_button_label,
    save_screen_caption,
    save_screen_title,
    set_editor_enabled,
    validate_button_label,
)
from app.services.screen_titles import (
    CAPTION_MAX_LENGTH,
    CAPTION_MAX_LINES,
    TITLE_MAX_LENGTH,
    can_hide_caption,
    has_caption,
    has_title,
    screen_caption_plain,
    screen_title,
)
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

owner_router = Router(name="screen_editor_owner")
shop_router = Router(name="screen_editor_shop")

_CODE_TO_COLOR = {"w": "", "p": "primary", "s": "success", "d": "danger"}
_RESET_CODE = "x"
# Where «⬅️ بازگشت» leads from the switches: the owner opened them from the admin home.
_SETTINGS_BACK = {"a": "nv:adm:home", "r": "res:st:sec:menu"}
_CANCEL_WORDS = ["انصراف", "لغو", "cancel"]

BTN_ENTRY = "✏️ حالت ویرایش منوها"
BTN_BACK = "⬅️ بازگشت"
BTN_BACK_TO_MENU = "↩️ بازگشت به منو"
BTN_EDIT_MAIN_MENU = "✏️ ویرایش منوی اصلی"
BTN_EDIT_TEXT = "✏️ تغییر متن"
BTN_RESET = "↺ رنگ پیش‌فرض"
BTN_CONTINUE = "✏️ ادامهٔ ویرایش"
TEXT_FEATURE_OFF_FOR_SHOP = "✏️ <b>حالت ویرایش منوها</b>\n\nاین قابلیت فعلاً توسط مدیر سیستم خاموش است."
TEXT_FEATURE_OFF = "قابلیت ویرایش منوها خاموش است."
TEXT_SHOP_BOT_ONLY = "حالت ویرایش فروشگاه فقط داخل ربات فروشگاه کار می‌کند."
TEXT_LABEL_SAVED = "✅ متن دکمه ذخیره شد."
TEXT_TITLE_SAVED = "✅ تیتر منو ذخیره شد."
TEXT_CAPTION_SAVED = "✅ توضیح منو ذخیره شد."
TEXT_CAPTION_HIDDEN = "✅ توضیح این منو دیگر نمایش داده نمی‌شود."
TEXT_TITLE_RESET = "✅ تیتر به متن پیش‌فرض برگشت."
TEXT_CAPTION_RESET = "✅ توضیح به متن پیش‌فرض برگشت."
BTN_HIDE_CAPTION = "🙈 نمایش نده"
BTN_RESET_TITLE = "↺ تیتر پیش‌فرض"
BTN_RESET_CAPTION = "↺ توضیح پیش‌فرض"
CAPTION_PREVIEW_LENGTH = 28
# Screen-text actions that change a setting right away, with the toast they answer.
_SCREEN_TEXT_ACTIONS = {"nr": ("title", TEXT_TITLE_RESET), "dr": ("caption", TEXT_CAPTION_RESET)}
TEXT_MENU_CHANGED = "این منو تغییر کرده؛ دوباره باز کنید."
TEXT_EDIT_MODE_ON = f"✅ حالت ویرایش روشن شد؛ تا {EDIT_MODE_MINUTES} دقیقه بعد از آخرین تغییر روشن می‌ماند."
TEXT_EDIT_MODE_OFF = "حالت ویرایش خاموش شد."
TEXT_EDIT_MODE_EXPIRED = f"⏱ زمان حالت ویرایش تمام شده. از «{BTN_ENTRY}» دوباره روشنش کنید."


class EditorStates(StatesGroup):
    owner_label = State()
    shop_label = State()


_EDITOR_STATES = frozenset({EditorStates.owner_label.state, EditorStates.shop_label.state})
# Views that open a text prompt; every other view leaves it.
_PROMPT_VIEWS = frozenset({"n", "d", "t"})


@dataclass(frozen=True, slots=True)
class EditRequest:
    audience: str
    view: str
    screen_id: str = ""
    index: int = -1
    code: str = ""


def entry_button(*, for_reseller: bool) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=BTN_ENTRY, callback_data=f"{EDIT_CALLBACK_ROOT}:{'r' if for_reseller else 'a'}:h")


def parse_request(data: str | None) -> EditRequest | None:
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != EDIT_CALLBACK_ROOT or parts[1] not in _SETTINGS_BACK:
        return None
    audience, view, rest = parts[1], parts[2], parts[3:]
    try:
        if view in {"h", "f1", "f0", "m1", "m0"} and not rest:
            return EditRequest(audience, view)
        if view in {"o", "n", "nr", "d", "dh", "dr"} and len(rest) == 1 and rest[0] in SCREENS:
            return EditRequest(audience, view, screen_id=rest[0])
        if view in {"b", "t"} and len(rest) == 2 and rest[0] in SCREENS:
            return EditRequest(audience, view, screen_id=rest[0], index=int(rest[1]))
        if view == "c" and len(rest) == 3 and rest[0] in SCREENS:
            return EditRequest(audience, view, screen_id=rest[0], index=int(rest[1]), code=rest[2])
    except ValueError:
        return None
    return None


def _cb(actor: EditorActor, *parts: object) -> str:
    return ":".join([EDIT_CALLBACK_ROOT, audience_code(actor), *map(str, parts)])


def _button(text: str, data: str, *, style: str | None = None) -> InlineKeyboardButton:
    if style:
        return InlineKeyboardButton(text=text, callback_data=data, style=style)
    return InlineKeyboardButton(text=text, callback_data=data)


def _swatch(color: str) -> str:
    return f"{COLOR_SWATCHES.get(color, '⚪')} {COLOR_NAMES.get(color, color)}"


# ── Views ────────────────────────────────────────────────────────────────────


def switches_view(actor: EditorActor, *, feature_on: bool, minutes_left: int) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    lines = ["✏️ <b>حالت ویرایش منوها</b>", ""]
    if not actor.for_reseller:
        state = "روشن ✅" if feature_on else "خاموش"
        lines.append(f"قابلیت برای همه: {state}")
        rows.append([_button(f"🔓 قابلیت ویرایش: {state}", _cb(actor, "f0" if feature_on else "f1"))])
    if feature_on:
        mine = f"روشن ({minutes_left} دقیقه)" if minutes_left else "خاموش"
        lines.append(f"حالت ویرایش شما: {mine}")
        lines.extend([
            "",
            "وقتی روشن است، زیر هر منو «✏️ ویرایش این منو» می‌آید.",
            f"اگر {EDIT_MODE_MINUTES} دقیقه تغییری ندهید، خودش خاموش می‌شود.",
        ])
        rows.append([_button(f"✏️ حالت ویرایش من: {mine}", _cb(actor, "m0" if minutes_left else "m1"))])
        if minutes_left:
            rows.append([_button(BTN_EDIT_MAIN_MENU, _cb(actor, "o", "c.main"))])
    rows.append([_button(BTN_BACK, _SETTINGS_BACK[audience_code(actor)])])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def panel_view(
    actor: EditorActor, spec: ScreenSpec, buttons: list[ScreenButton], ui: dict[str, str], *, minutes_left: int
) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button(f"{_button_marker(actor, button, ui)} {button.text}", _cb(actor, "b", spec.id, index))]
        for index, button in enumerate(buttons)
        if _editable_parts(actor, button, ui) != (False, False)
    ]
    text_rows = []
    if has_title(spec.id):
        text_rows.append([_button(f"📝 تیتر: {screen_title(ui, spec.id)}", _cb(actor, "n", spec.id))])
    if has_caption(spec.id):
        text_rows.append([_button(f"📄 توضیح: {_caption_preview(ui, spec.id)}", _cb(actor, "d", spec.id))])
    rows = text_rows + rows
    rows.append([_button(BTN_BACK_TO_MENU, spec.reopen_callback)])
    hint = (
        "روی تیتر، توضیح یا یک دکمه بزنید تا آن را عوض کنید."
        if len(rows) > 1
        else "در این منو چیزی برای ویرایش شما نیست."
    )
    text = f"✏️ <b>ویرایش: {html.escape(spec.title)}</b>\n{hint}\n⏱ حالت ویرایش تا {minutes_left} دقیقهٔ دیگر روشن است."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _caption_preview(ui: dict[str, str], screen_id: str) -> str:
    caption = " ".join(screen_caption_plain(ui, screen_id).split())
    if not caption:
        return "(نمایش داده نمی‌شود)"
    return caption if len(caption) <= CAPTION_PREVIEW_LENGTH else caption[: CAPTION_PREVIEW_LENGTH - 1] + "…"


def button_view(
    actor: EditorActor, spec: ScreenSpec, index: int, button: ScreenButton, ui: dict[str, str]
) -> tuple[str, InlineKeyboardMarkup]:
    label_ok, color_ok = _editable_parts(actor, button, ui)
    lines = [f"✏️ <b>{html.escape(spec.title)}</b> › {html.escape(button.text)}", ""]
    rows: list[list[InlineKeyboardButton]] = []
    if label_ok:
        rows.append([_button(BTN_EDIT_TEXT, _cb(actor, "t", spec.id, index))])
    if color_ok:
        item = find_color_item(ui, button.style_id, for_reseller=actor.for_reseller)
        lines.append(f"رنگ فعلی: {_swatch(item.color)} · پیش‌فرض: {_swatch(item.default)}")
        shared = shared_button_count(item.style_id)
        if shared > 1:
            lines.append(f"⚠️ این رنگ بین {shared} دکمه مشترک است و همه با هم عوض می‌شوند.")
        rows.extend(_color_rows(actor, spec.id, index, item))
    if not label_ok:
        lines.append("متن این دکمه از اینجا قابل ویرایش نیست.")
    rows.append([_button(BTN_BACK, _cb(actor, "o", spec.id))])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _color_rows(actor: EditorActor, screen_id: str, index: int, item: ColorItem) -> list[list[InlineKeyboardButton]]:
    buttons = [
        _button(f"{'✓ ' if color == item.color else ''}{_swatch(color)}", _cb(actor, "c", screen_id, index, code), style=color or None)
        for code, color in _CODE_TO_COLOR.items()
    ]
    return [buttons[0:2], buttons[2:4], [_button(BTN_RESET, _cb(actor, "c", screen_id, index, _RESET_CODE))]]


def _editable_parts(actor: EditorActor, button: ScreenButton, ui: dict[str, str]) -> tuple[bool, bool]:
    label_ok = can_edit_label(actor, button.label_key)
    color_ok = False
    if button.style_id:
        try:
            find_color_item(ui, button.style_id, for_reseller=actor.for_reseller)
            color_ok = True
        except ValueError:
            color_ok = False
    return label_ok, color_ok


def _button_marker(actor: EditorActor, button: ScreenButton, ui: dict[str, str]) -> str:
    _label_ok, color_ok = _editable_parts(actor, button, ui)
    if not color_ok:
        return "✏️"
    return COLOR_SWATCHES.get(find_color_item(ui, button.style_id, for_reseller=actor.for_reseller).color, "⚪")


# ── Shared handling ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Screen:
    spec: ScreenSpec
    buttons: list[ScreenButton]
    ui: dict[str, str]


async def _load_screen(session: AsyncSession, actor: EditorActor, screen_id: str) -> _Screen:
    spec = SCREENS.get(screen_id)
    if spec is None or spec.audience.value not in actor.audiences:
        raise ValueError(feedback.INVALID)
    ui = await get_all_settings(session, reseller_id=actor.settings_scope)
    profile = await get_reseller_profile(session, actor.shop_id) if actor.for_reseller else None
    return _Screen(spec, screen_buttons(screen_id, ScreenContext(ui=ui, profile=profile)), ui)


def _button_at(screen: _Screen, index: int) -> ScreenButton:
    if not 0 <= index < len(screen.buttons):
        raise ValueError(TEXT_MENU_CHANGED)
    return screen.buttons[index]


async def _show(callback: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    text, markup = view
    await safe_edit_text(callback.message, text, reply_markup=markup)


async def _switches(callback: CallbackQuery, session: AsyncSession, actor: EditorActor, request: EditRequest) -> None:
    if request.view in {"f1", "f0"}:
        if actor.for_reseller:
            raise ValueError(feedback.INVALID)
        await set_editor_enabled(session, request.view == "f1")
    feature_on = await is_editor_enabled(session)
    toast = None
    if request.view == "m1":
        if not feature_on:
            raise ValueError(TEXT_FEATURE_OFF)
        edit_mode_store.activate(actor)
        toast = TEXT_EDIT_MODE_ON
    elif request.view == "m0" or not feature_on:
        edit_mode_store.deactivate(actor)
        toast = TEXT_EDIT_MODE_OFF if request.view == "m0" else None
    await callback.answer(toast)
    if actor.for_reseller and not feature_on:
        rows = [[_button(BTN_BACK, _SETTINGS_BACK["r"])]]
        await _show(callback, (TEXT_FEATURE_OFF_FOR_SHOP, InlineKeyboardMarkup(inline_keyboard=rows)))
        return
    await _show(callback, switches_view(actor, feature_on=feature_on, minutes_left=edit_mode_store.minutes_left(actor)))


async def _screen_views(
    callback: CallbackQuery, session: AsyncSession, actor: EditorActor, request: EditRequest, state: FSMContext
) -> None:
    if not await is_editor_enabled(session):
        raise ValueError(TEXT_FEATURE_OFF)
    if not edit_mode_store.is_active(actor):
        raise ValueError(TEXT_EDIT_MODE_EXPIRED)
    screen = await _load_screen(session, actor, request.screen_id)
    if request.view not in _PROMPT_VIEWS and await state.get_state() in _EDITOR_STATES:
        # Back out of a text prompt: a later message must not be saved as its answer.
        await state.set_state(None)
    if request.view == "o":
        await callback.answer()
        minutes_left = edit_mode_store.minutes_left(actor)
        await _show(callback, panel_view(actor, screen.spec, screen.buttons, screen.ui, minutes_left=minutes_left))
        return
    if request.view in {"n", "d"}:
        await _ask_screen_text(callback, actor, screen, state, part="title" if request.view == "n" else "caption")
        return
    if request.view in {"nr", "dr", "dh"}:
        await _change_screen_text(callback, session, actor, screen, request.view)
        return
    button = _button_at(screen, request.index)
    if request.view == "t":
        await _ask_label(callback, actor, screen, request.index, button, state)
        return
    if request.view == "c":
        screen = await _save_color(session, actor, screen, button, request.code)
        button = _button_at(screen, request.index)
        await callback.answer(feedback.COLOR_SAVED)
    else:
        await callback.answer()
    await _show(callback, button_view(actor, screen.spec, request.index, button, screen.ui))


async def _save_color(
    session: AsyncSession, actor: EditorActor, screen: _Screen, button: ScreenButton, code: str
) -> _Screen:
    if not _editable_parts(actor, button, screen.ui)[1]:
        raise ValueError("رنگ این دکمه قابل ویرایش نیست.")
    if code == _RESET_CODE:
        await reset_button_color(session, style_id=button.style_id, for_reseller=actor.for_reseller, shop_id=actor.shop_id)
    elif code in _CODE_TO_COLOR:
        await set_button_color(
            session, style_id=button.style_id, color=_CODE_TO_COLOR[code], for_reseller=actor.for_reseller, shop_id=actor.shop_id
        )
    else:
        raise ValueError(feedback.INVALID)
    edit_mode_store.extend(actor)
    return await _load_screen(session, actor, screen.spec.id)


async def _ask_label(
    callback: CallbackQuery, actor: EditorActor, screen: _Screen, index: int, button: ScreenButton, state: FSMContext
) -> None:
    if not can_edit_label(actor, button.label_key):
        raise ValueError("متن این دکمه قابل ویرایش نیست.")
    await state.set_state(EditorStates.shop_label if actor.for_reseller else EditorStates.owner_label)
    await state.update_data(ed_screen=screen.spec.id, ed_kind="label", ed_index=index, ed_key=button.label_key)
    await callback.answer()
    prompt = (
        f"✏️ متن جدید دکمهٔ «{html.escape(button.text)}» را بفرستید.\n"
        f"یک خط، حداکثر {LABEL_MAX_LENGTH} کاراکتر.\n"
        "برای انصراف: انصراف"
    )
    rows = [[_button(BTN_BACK, _cb(actor, "b", screen.spec.id, index))]]
    await _show(callback, (prompt, InlineKeyboardMarkup(inline_keyboard=rows)))


async def _ask_screen_text(
    callback: CallbackQuery, actor: EditorActor, screen: _Screen, state: FSMContext, *, part: str
) -> None:
    spec_id = screen.spec.id
    if not (has_title(spec_id) if part == "title" else has_caption(spec_id)):
        raise ValueError("تیتر این منو قابل ویرایش نیست." if part == "title" else "توضیح این منو قابل ویرایش نیست.")
    await state.set_state(EditorStates.shop_label if actor.for_reseller else EditorStates.owner_label)
    await state.update_data(ed_screen=spec_id, ed_kind=part, ed_index=None, ed_key=None)
    await callback.answer()
    if part == "title":
        prompt = (
            f"📝 تیتر جدید منوی «{html.escape(screen_title(screen.ui, spec_id))}» را بفرستید.\n"
            f"یک خط، حداکثر {TITLE_MAX_LENGTH} کاراکتر، بدون &lt; و &gt; و &amp;. ایموجی پریمیوم هم می‌شود.\n"
            "برای انصراف: انصراف"
        )
        rows = [[_button(BTN_RESET_TITLE, _cb(actor, "nr", spec_id))]]
    else:
        current = screen_caption_plain(screen.ui, spec_id)
        shown = html.escape(current) if current else "(نمایش داده نمی‌شود)"
        prompt = (
            f"📄 توضیح زیر تیتر منوی «{html.escape(screen_title(screen.ui, spec_id))}»:\n"
            f"<blockquote>{shown}</blockquote>\n"
            f"متن جدید را بفرستید؛ حداکثر {CAPTION_MAX_LINES} خط و {CAPTION_MAX_LENGTH} کاراکتر. ایموجی پریمیوم هم می‌شود.\n"
            "برای انصراف: انصراف"
        )
        rows = [[_button(BTN_HIDE_CAPTION, _cb(actor, "dh", spec_id))]] if can_hide_caption(spec_id) else []
        rows.append([_button(BTN_RESET_CAPTION, _cb(actor, "dr", spec_id))])
    rows.append([_button(BTN_BACK, _cb(actor, "o", spec_id))])
    await _show(callback, (prompt, InlineKeyboardMarkup(inline_keyboard=rows)))


async def _change_screen_text(
    callback: CallbackQuery, session: AsyncSession, actor: EditorActor, screen: _Screen, view: str
) -> None:
    """Reset the title or caption, or hide the caption, then show the menu's panel again."""
    spec = screen.spec
    if view == "dh":
        await hide_screen_caption(session, actor, screen_id=spec.id, audience=spec.audience.value)
        toast = TEXT_CAPTION_HIDDEN
    else:
        part, toast = _SCREEN_TEXT_ACTIONS[view]
        await reset_screen_text(session, actor, screen_id=spec.id, audience=spec.audience.value, part=part)
    edit_mode_store.extend(actor)
    screen = await _load_screen(session, actor, spec.id)
    await callback.answer(toast)
    minutes_left = edit_mode_store.minutes_left(actor)
    await _show(callback, panel_view(actor, screen.spec, screen.buttons, screen.ui, minutes_left=minutes_left))


async def _handle(
    callback: CallbackQuery, session: AsyncSession, actor: EditorActor, request: EditRequest, state: FSMContext
) -> None:
    try:
        if request.view in {"h", "f1", "f0", "m1", "m0"}:
            await _switches(callback, session, actor, request)
        else:
            await _screen_views(callback, session, actor, request, state)
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)


async def _is_menu_tap(message: Message, session: AsyncSession, actor: EditorActor) -> bool:
    """A typed new name differs from every visible label (another emoji, a premium emoji or other
    words), so it is saved even when its letters match a menu label, e.g. «👍 کاربران»."""
    return is_menu_tap(message, await get_all_settings(session, reseller_id=actor.settings_scope))


async def _save_label_message(message: Message, state: FSMContext, session: AsyncSession, actor: EditorActor) -> None:
    data = await state.get_data()
    screen_id, index, key = data.get("ed_screen"), data.get("ed_index"), data.get("ed_key")
    raw = message.text or ""
    if kb.is_cancel_text(raw.strip()) or not screen_id:
        await state.set_state(None)
        if data.get("ed_kind") == "label" and index is not None:
            await _answer_with_button(message, session, actor, screen_id, int(index))
            return
        await _answer_with_panel(message, session, actor, screen_id, feedback.CANCELLED)
        return
    if await _is_menu_tap(message, session, actor):
        # The person left the editor by tapping a menu button: stop waiting and let navigation run.
        await state.set_state(None)
        raise SkipHandler()
    if data.get("ed_kind") in {"title", "caption"}:
        await _save_screen_text_message(message, state, session, actor, screen_id, part=data["ed_kind"])
        return
    try:
        screen = await _load_screen(session, actor, screen_id)
        button = _button_at(screen, int(index))
        if button.label_key != key:
            raise ValueError(TEXT_MENU_CHANGED)
        label = validate_button_label(
            raw,
            other_labels=[b.text.strip() for i, b in enumerate(screen.buttons) if i != int(index)],
            reserved=[*_CANCEL_WORDS, kb._home_label(screen.ui), kb._back_label(screen.ui)],
        )
        stored = pack_setting_from_message(key, message) if message.entities and label == raw.strip() else label
        await save_button_label(session, actor, label_key=key, stored_value=stored)
        edit_mode_store.extend(actor)
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    await state.set_state(None)
    await _answer_with_panel(message, session, actor, screen_id, TEXT_LABEL_SAVED)


async def _save_screen_text_message(
    message: Message, state: FSMContext, session: AsyncSession, actor: EditorActor, screen_id: str, *, part: str
) -> None:
    spec = SCREENS.get(screen_id)
    save = save_screen_title if part == "title" else save_screen_caption
    try:
        if spec is None:
            raise ValueError(TEXT_MENU_CHANGED)
        await save(
            session, actor, screen_id=screen_id, audience=spec.audience.value,
            text=message.text or "", entities=message.entities,
        )
        edit_mode_store.extend(actor)
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    await state.set_state(None)
    await _answer_with_panel(message, session, actor, screen_id, TEXT_TITLE_SAVED if part == "title" else TEXT_CAPTION_SAVED)


async def _answer_with_panel(
    message: Message, session: AsyncSession, actor: EditorActor, screen_id: str | None, notice: str
) -> None:
    """Confirm, then show the edit panel of the same menu with its new texts."""
    try:
        if not screen_id or not edit_mode_store.is_active(actor):
            raise ValueError(TEXT_EDIT_MODE_EXPIRED)
        screen = await _load_screen(session, actor, screen_id)
    except ValueError:
        await message.answer(notice, reply_markup=_after_label_markup(actor, screen_id))
        return
    text, markup = panel_view(
        actor, screen.spec, screen.buttons, screen.ui, minutes_left=edit_mode_store.minutes_left(actor)
    )
    await message.answer(f"{notice}\n\n{text}", reply_markup=markup)


async def _answer_with_button(
    message: Message, session: AsyncSession, actor: EditorActor, screen_id: str, index: int
) -> None:
    """Typed cancel of a label prompt shows the button's screen, like its «بازگشت»."""
    try:
        if not edit_mode_store.is_active(actor):
            raise ValueError(TEXT_EDIT_MODE_EXPIRED)
        screen = await _load_screen(session, actor, screen_id)
        button = _button_at(screen, index)
    except ValueError:
        await _answer_with_panel(message, session, actor, screen_id, feedback.CANCELLED)
        return
    text, markup = button_view(actor, screen.spec, index, button, screen.ui)
    await message.answer(f"{feedback.CANCELLED}\n\n{text}", reply_markup=markup)


def _after_label_markup(actor: EditorActor, screen_id: str | None) -> InlineKeyboardMarkup | None:
    spec = SCREENS.get(screen_id or "")
    if spec is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(BTN_BACK_TO_MENU, spec.reopen_callback), _button(BTN_CONTINUE, _cb(actor, "o", spec.id))]]
    )


# ── Routers ──────────────────────────────────────────────────────────────────


# Literal prefixes (EDIT_CALLBACK_ROOT) keep the handlers visible to the callback contract check.
@owner_router.callback_query(F.data.startswith("ed:a:"))
async def owner_editor(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    request = parse_request(callback.data)
    if request is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await _handle(callback, session, EditorActor(user_id=int(db_user.id), shop_id=None), request, state)


@owner_router.message(EditorStates.owner_label)
async def owner_label_text(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser) -> None:
    await _save_label_message(message, state, session, EditorActor(user_id=int(db_user.id), shop_id=None))


async def _shop_actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> EditorActor | None:
    if not is_reseller_bot:
        return None
    shop_id = await shop_settings_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return EditorActor(user_id=int(db_user.id), shop_id=shop_id) if shop_id else None


@shop_router.callback_query(F.data.startswith("ed:r:"))
async def shop_editor(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    request = parse_request(callback.data)
    if request is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        text = TEXT_SHOP_BOT_ONLY if not is_reseller_bot else feedback.NO_SHOP_SETTINGS_ACCESS
        await callback.answer(text, show_alert=True)
        return
    await _handle(callback, session, actor, request, state)


@shop_router.message(EditorStates.shop_label)
async def shop_label_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await state.set_state(None)
        await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
        return
    await _save_label_message(message, state, session, actor)
