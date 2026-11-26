"""The bot step that adds buttons under a staff message: a question, a checklist of one to four, colours.

After the text, the bot asks «دکمه پایین پیام باشه؟». «ارسال بدون دکمه» continues at once; «بله» shows
the checklist (bot screens with the labels of the sender's menus, «تمدید سرویس» in a direct message) and
the shop's saved sets. When the sender may add links, «🔗 لینک دلخواه» asks for the button text and then
an https link; the link is checked at once and joins the checklist as a chosen button. After
«ادامه» each chosen button is shown in its colour and a tap gives it the next one, the same four
colours the web form offers. A saved set (made on the web, where links and colours are set) is taken
as it is. The flow that asked continues through the finisher it registered, with the chosen buttons;
the buttons are validated there, against the sender's shop and permissions, before anything is sent.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.nav_input import finish_text_step
from app.db.models import BotUser
from app.services.button_styles import STYLE_OPTIONS, normalize_style
from app.services.message_buttons import (
    BUTTONS_MAX,
    TARGET_LINK,
    ButtonInput,
    ButtonRules,
    ScreenLabels,
    list_presets,
    make_button,
    preset_inputs,
    sender_screen_labels,
    shop_may_link,
    target_options,
)

router = Router(name="message_button_picker")

PREFIX = "mbp:"
CB_YES = "mbp:y"
CB_NO = "mbp:n"
CB_DONE = "mbp:ok"
CB_TOGGLE = "mbp:t:"
CB_PRESET = "mbp:p:"
CB_COLOR = "mbp:c:"
CB_SEND = "mbp:s"
CB_LINK = "mbp:l"
LINK_KEY_PREFIX = "u"
LINK_ASK_LABEL = "🔗 متن دکمهٔ لینک را بفرستید (حداکثر ۴۰ نویسه)؛ ایموجی هم مجاز است."
LINK_ASK_URL = "🔗 حالا لینک دکمه را بفرستید؛ باید با https:// شروع شود."
LINK_ADDED = "دکمهٔ لینک اضافه شد."
LINK_BUTTON = "🔗 لینک دلخواه"
QUESTION = "دکمه پایین پیام باشه؟"
CHECKLIST = f"دکمه‌های زیر پیام را انتخاب کنید (۱ تا {BUTTONS_MAX})؛ قالب‌های ذخیره‌شده پایین فهرست‌اند."
TOO_MANY = f"حداکثر {BUTTONS_MAX} دکمه"
NONE_CHOSEN = "دست‌کم یک دکمه انتخاب کنید یا «بدون دکمه» را بزنید"
EXPIRED = "این انتخاب منقضی شده؛ دوباره پیام را بفرستید"
COLORS = "رنگ دکمه‌ها: روی هر دکمه بزنید تا رنگ بعدی را بگیرد (سفید، آبی، سبز، قرمز). پیام با همین رنگ‌ها ارسال می‌شود."
# The web form's colours, in the order a tap walks through them ("" is Telegram's default white).
# Right-to-left mark: the checklist text starts with a symbol (✅/⬜), so Telegram would lay it out left to right and
# put the checkbox between a premium emoji (always drawn first) and the label. Starting with RLM keeps the line
# right to left: the checkbox stays first on the right, the label next, the premium emoji at the far end.
RTL_MARK = "\u200f"
STYLE_CYCLE: tuple[str, ...] = tuple(value for value, _label, _css in STYLE_OPTIONS)
STYLE_NAMES: dict[str, str] = {value: label for value, label, _css in STYLE_OPTIONS}

# finisher(callback, state, session, db_user, buttons, **middleware data)
Finisher = Callable[..., Awaitable[None]]
_FINISHERS: dict[str, Finisher] = {}


class PickerStates(StatesGroup):
    picking = State()
    link_label = State()
    link_url = State()


def register_flow(name: str, finisher: Finisher) -> None:
    """A flow that asks for buttons; ``finisher`` continues it with the chosen buttons."""
    _FINISHERS[name] = finisher


def question_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، دکمه بگذار", callback_data=CB_YES)],
        [InlineKeyboardButton(text="➡️ ارسال بدون دکمه", callback_data=CB_NO)],
    ])


def option_labels(*, renew: bool, labels: ScreenLabels | None, links: dict[str, dict]) -> dict[str, str]:
    """Checklist key -> text: the bot screens (menu labels), then each link button added in this flow."""
    out = dict(target_options(renew=renew, links=False, labels=labels))
    out.update({key: f"🔗 {link.get('label', '')}" for key, link in links.items()})
    return out


def option_icons(labels: ScreenLabels | None) -> dict[str, str]:
    """Checklist key -> premium emoji id of the screen's menu label, for the screens that have one."""
    return {key: label.icon for key, label in (labels or {}).items() if label.icon}


def _icon_extra(icon: str | None) -> dict[str, str]:
    return {"icon_custom_emoji_id": icon} if icon else {}


def checklist_markup(
    selected: list[str],
    *,
    renew: bool,
    presets: list[tuple[int, str]],
    labels: ScreenLabels | None = None,
    links: dict[str, dict] | None = None,
    may_link: bool = False,
) -> InlineKeyboardMarkup:
    """Two columns of ✅/⬜ targets and added links, «لینک دلخواه», one row per saved set, then «ادامه»."""
    icons = option_icons(labels)
    options = [
        InlineKeyboardButton(
            text=f"{RTL_MARK}{'✅' if key in selected else '⬜'} {label}", callback_data=f"{CB_TOGGLE}{key}",
            **_icon_extra(icons.get(key)),
        )
        for key, label in option_labels(renew=renew, labels=labels, links=links or {}).items()
    ]
    rows = [options[i:i + 2] for i in range(0, len(options), 2)]
    if may_link:
        rows.append([InlineKeyboardButton(text=LINK_BUTTON, callback_data=CB_LINK)])
    rows += [[InlineKeyboardButton(text=f"📚 قالب: {name}", callback_data=f"{CB_PRESET}{pid}")] for pid, name in presets]
    rows.append([
        InlineKeyboardButton(text=f"ادامه ({len(selected)})", callback_data=CB_DONE),
        InlineKeyboardButton(text="بدون دکمه", callback_data=CB_NO),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def next_style(style: str) -> str:
    """The colour after ``style`` in the web form's order; an unknown value starts over at white."""
    current = normalize_style(style)
    return STYLE_CYCLE[(STYLE_CYCLE.index(current) + 1) % len(STYLE_CYCLE)]


def colors_markup(
    selected: list[str], styles: dict[str, str], *, labels: dict[str, str], icons: dict[str, str] | None = None
) -> InlineKeyboardMarkup:
    """Each chosen button drawn in its colour and premium emoji (a tap changes the colour), then «ارسال» and «بازگشت»."""
    rows = []
    for key in selected:
        style = normalize_style(styles.get(key))
        extra = {"style": style} if style else {}
        text = f"{labels.get(key, key)} · {STYLE_NAMES[style]}"
        rows.append([InlineKeyboardButton(
            text=text, callback_data=f"{CB_COLOR}{key}", **extra, **_icon_extra((icons or {}).get(key))
        )])
    rows.append([
        InlineKeyboardButton(text="✅ ارسال", callback_data=CB_SEND),
        InlineKeyboardButton(text="⬅️ بازگشت", callback_data=CB_YES),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def toggle(selected: list[str], key: str) -> list[str]:
    """``selected`` with ``key`` switched; ``ValueError`` past the limit. Order is the order tapped."""
    if key in selected:
        return [k for k in selected if k != key]
    if len(selected) >= BUTTONS_MAX:
        raise ValueError(TOO_MANY)
    return [*selected, key]


async def ask_for_buttons(
    message: Message, state: FSMContext, *, flow: str, shop_id: int | None, renew: bool = False
) -> None:
    """Ask the question in place of the text prompt (the same panel); the flow's own data stays in ``state``."""
    await finish_text_step(message, state, text=QUESTION, inline=question_markup())
    await state.set_state(PickerStates.picking)
    await state.update_data(
        mbp_flow=flow, mbp_shop=shop_id, mbp_renew=renew, mbp_selected=[], mbp_styles={}, mbp_links={}
    )


async def _checklist(state: FSMContext, session: AsyncSession) -> InlineKeyboardMarkup:
    data = await state.get_data()
    shop_id = data.get("mbp_shop")
    presets = [(int(p.id), p.name) for p in await list_presets(session, shop_id)]
    return checklist_markup(
        list(data.get("mbp_selected") or []),
        renew=bool(data.get("mbp_renew")),
        presets=presets,
        labels=await sender_screen_labels(session, shop_id),
        links=dict(data.get("mbp_links") or {}),
        may_link=await shop_may_link(session, shop_id),
    )


async def _show_checklist(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    markup = await _checklist(state, session)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(CHECKLIST, reply_markup=markup)


async def _labels(state: FSMContext, session: AsyncSession) -> dict[str, str]:
    data = await state.get_data()
    labels = await sender_screen_labels(session, data.get("mbp_shop"))
    return option_labels(renew=bool(data.get("mbp_renew")), labels=labels, links=dict(data.get("mbp_links") or {}))


async def _finish(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
                  buttons: list[ButtonInput], extra: dict) -> None:
    data = await state.get_data()
    finisher = _FINISHERS.get(str(data.get("mbp_flow") or ""))
    if finisher is None:
        await state.clear()
        await callback.answer(EXPIRED, show_alert=True)
        return
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    await message.edit_reply_markup(reply_markup=None)
    await finisher(callback, state, session, db_user, buttons, **extra)


def _extra(kwargs: dict) -> dict:
    return {k: kwargs[k] for k in ("is_reseller_bot", "reseller_owner_id") if k in kwargs}


@router.callback_query(F.data == CB_YES, PickerStates.picking)
async def on_yes(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _show_checklist(callback, state, session)


@router.callback_query(F.data.startswith(CB_TOGGLE), PickerStates.picking)
async def on_toggle(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    key = (callback.data or "")[len(CB_TOGGLE):]
    allowed = {k for k, _l in target_options(renew=bool(data.get("mbp_renew")), links=False)} | set(data.get("mbp_links") or {})
    if key not in allowed:
        await callback.answer(EXPIRED, show_alert=True)
        return
    try:
        selected = toggle(list(data.get("mbp_selected") or []), key)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(mbp_selected=selected)
    await callback.answer()
    await _show_checklist(callback, state, session)


async def _show_colors(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    icons = option_icons(await sender_screen_labels(session, data.get("mbp_shop")))
    markup = colors_markup(
        list(data.get("mbp_selected") or []), dict(data.get("mbp_styles") or {}),
        labels=await _labels(state, session), icons=icons,
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(COLORS, reply_markup=markup)


@router.callback_query(F.data == CB_DONE, PickerStates.picking)
async def on_done(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«ادامه» on the checklist opens the colour step."""
    if not (await state.get_data()).get("mbp_selected"):
        await callback.answer(NONE_CHOSEN, show_alert=True)
        return
    await callback.answer()
    await _show_colors(callback, state, session)


@router.callback_query(F.data.startswith(CB_COLOR), PickerStates.picking)
async def on_color(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    key = (callback.data or "")[len(CB_COLOR):]
    if key not in (data.get("mbp_selected") or []):
        await callback.answer(EXPIRED, show_alert=True)
        return
    styles = dict(data.get("mbp_styles") or {})
    styles[key] = next_style(styles.get(key, ""))
    await state.update_data(mbp_styles=styles)
    await callback.answer(STYLE_NAMES[styles[key]])
    await _show_colors(callback, state, session)


@router.callback_query(F.data == CB_SEND, PickerStates.picking)
async def on_send(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    data = await state.get_data()
    selected = list(data.get("mbp_selected") or [])
    if not selected:
        await callback.answer(NONE_CHOSEN, show_alert=True)
        return
    styles = dict(data.get("mbp_styles") or {})
    buttons = chosen_inputs(selected, styles, dict(data.get("mbp_links") or {}))
    await _finish(callback, state, session, db_user, buttons, _extra(kwargs))


def chosen_inputs(selected: list[str], styles: dict[str, str], links: dict[str, dict]) -> list[ButtonInput]:
    """The chosen buttons in order: a link key becomes its link button, any other key a bot screen."""
    out = []
    for key in selected:
        style = normalize_style(styles.get(key))
        link = links.get(key)
        if link is not None:
            out.append(ButtonInput(TARGET_LINK, str(link.get("label") or ""), str(link.get("url") or ""), style))
        else:
            out.append(ButtonInput(key, style=style))
    return out


@router.callback_query(F.data == CB_LINK, PickerStates.picking)
async def on_link(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«لینک دلخواه»: ask for the button text (the sender's permission is checked again here)."""
    data = await state.get_data()
    if not await shop_may_link(session, data.get("mbp_shop")):
        await callback.answer(EXPIRED, show_alert=True)
        return
    if len(data.get("mbp_selected") or []) >= BUTTONS_MAX:
        await callback.answer(TOO_MANY, show_alert=True)
        return
    await callback.answer()
    await state.set_state(PickerStates.link_label)
    if isinstance(callback.message, Message):
        back = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data=CB_YES)]])
        await callback.message.edit_text(LINK_ASK_LABEL, reply_markup=back)


@router.message(PickerStates.link_label)
async def on_link_label(message: Message, state: FSMContext) -> None:
    label = (message.text or "").strip()
    try:
        # Checked with a placeholder link; the real one is checked in the next step.
        make_button(ButtonInput(TARGET_LINK, label, "https://example.com"), ButtonRules(allow_links=True))
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    if not label:
        await message.answer(LINK_ASK_LABEL)
        return
    await state.update_data(mbp_link_label=label)
    await state.set_state(PickerStates.link_url)
    await message.answer(LINK_ASK_URL)


@router.message(PickerStates.link_url)
async def on_link_url(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    url = (message.text or "").strip()
    try:
        make_button(ButtonInput(TARGET_LINK, str(data.get("mbp_link_label") or ""), url), ButtonRules(allow_links=True))
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    links = dict(data.get("mbp_links") or {})
    selected = list(data.get("mbp_selected") or [])
    if len(selected) >= BUTTONS_MAX:
        await message.answer(TOO_MANY)
    else:
        key = new_link_key(links)
        links[key] = {"label": str(data.get("mbp_link_label") or ""), "url": url}
        selected.append(key)
    await state.update_data(mbp_links=links, mbp_selected=selected, mbp_link_label=None)
    await state.set_state(PickerStates.picking)
    await message.answer(f"{LINK_ADDED}\n{CHECKLIST}", reply_markup=await _checklist(state, session))


def new_link_key(links: dict[str, dict]) -> str:
    """A checklist key for the next link button (``u1``, ``u2`` …), never one already used."""
    number = 1
    while f"{LINK_KEY_PREFIX}{number}" in links:
        number += 1
    return f"{LINK_KEY_PREFIX}{number}"


@router.callback_query(F.data == CB_YES, PickerStates.link_label)
async def on_link_back(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.set_state(PickerStates.picking)
    await callback.answer()
    await _show_checklist(callback, state, session)


@router.callback_query(F.data == CB_NO, PickerStates.picking)
async def on_no(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    await _finish(callback, state, session, db_user, [], _extra(kwargs))


@router.callback_query(F.data.regexp(r"^mbp:p:\d{1,10}$"), PickerStates.picking)
async def on_preset(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    data = await state.get_data()
    # The preset is read with the shop the flow started in, never one named in the callback.
    buttons = await preset_inputs(session, data.get("mbp_shop"), int((callback.data or "")[len(CB_PRESET):]))
    if not buttons:
        await callback.answer(EXPIRED, show_alert=True)
        return
    await _finish(callback, state, session, db_user, buttons, _extra(kwargs))


@router.callback_query(F.data.startswith(PREFIX))
async def on_stale(callback: CallbackQuery) -> None:
    """A tap on an old question or checklist after the flow ended."""
    await callback.answer(EXPIRED, show_alert=True)
