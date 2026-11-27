"""The bot step that adds buttons under a staff message: a question, a checklist of one to four, colours.

After the text, the bot asks «دکمه پایین پیام باشه؟». «ارسال بدون دکمه» continues at once; «بله» shows
the checklist (bot screens with the labels of the sender's menus, «تمدید سرویس» in a direct message) and
the shop's saved sets. When the sender may add links, «🔗 لینک دلخواه» asks for the button text and then
an https link; the link is checked at once and joins the checklist as a chosen button. After
«ادامه» each chosen button is shown in its colour and a tap gives it the next one, the same four
colours the web form offers. A saved set (made on the web, where links and colours are set) is taken
as it is. The flow that asked continues through the finisher it registered, with the chosen buttons;
the buttons are validated there, against the sender's shop and permissions, before anything is sent.

Every step has the shared «بازگشت» (``kb.back_button``: the bot's own label, colour and premium emoji)
on a row of its own, and it goes exactly one step back: links → link text → checklist, colours →
checklist, checklist → the question, question → the text prompt the flow asked before it (asked again
as it was). A flow that opens straight on the checklist (``open_checklist``, e.g. the buttons under an
FAQ answer) registers where the checklist's «بازگشت» leads.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.nav_input import CANCEL_CODE_KEY, PROMPT_TEXT_KEY, RETURN_TO_KEY, ask_text, finish_text_step
from app.db.models import BotUser
from app.services.button_styles import STYLE_OPTIONS, normalize_style
from app.services.rich_text import button_display_text, button_icon_custom_emoji_id, pack_rich_text
from app.services.users import current_ui_snapshot
from app.services.message_buttons import (
    BUTTONS_MAX,
    SCREENS,
    TARGET_LINK,
    TARGET_PLAN,
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
CB_BACK = "mbp:b"
CB_QUESTION = "mbp:q"
CB_LINK_BACK = "mbp:lb"
ENTRY_QUESTION = "question"
ENTRY_CHECKLIST = "checklist"
SEND_LABEL = "✅ ارسال"
LINK_KEY_PREFIX = "u"
LINK_ASK_LABEL = "🔗 متن دکمهٔ لینک را بفرستید (حداکثر ۴۰ نویسه). ایموجی و ایموجی پریمیوم هم مجاز است؛ اولین ایموجی پریمیوم آیکن دکمه می‌شود."
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
# Telegram always draws a button's premium emoji before its text. The checkbox (✅/⬜) therefore goes at the end of
# the text, so it never sits between the premium emoji and the label; the leading right-to-left mark keeps the whole
# line right to left: premium emoji, label, then the checkbox.
RTL_MARK = "\u200f"
STYLE_CYCLE: tuple[str, ...] = tuple(value for value, _label, _css in STYLE_OPTIONS)
STYLE_NAMES: dict[str, str] = {value: label for value, label, _css in STYLE_OPTIONS}

# finisher(callback, state, session, db_user, buttons, **middleware data)
Finisher = Callable[..., Awaitable[None]]
# back(callback, state, session, db_user, **middleware data): the screen before an ``open_checklist`` flow.
Back = Callable[..., Awaitable[None]]
_FINISHERS: dict[str, Finisher] = {}
_BACKS: dict[str, Back] = {}
_SEND_LABELS: dict[str, str] = {}


class PickerStates(StatesGroup):
    picking = State()
    link_label = State()
    link_url = State()


def register_flow(name: str, finisher: Finisher, *, back: Back | None = None, send_label: str = SEND_LABEL) -> None:
    """A flow that asks for buttons; ``finisher`` continues it with the chosen buttons.

    ``back`` is where the first screen's «بازگشت» leads for a flow opened with ``open_checklist``;
    ``send_label`` names the last step's button («ارسال», or «ذخیره» for a flow that saves).
    """
    _FINISHERS[name] = finisher
    _SEND_LABELS[name] = send_label
    if back is not None:
        _BACKS[name] = back


def back_row(callback_data: str) -> list[InlineKeyboardButton]:
    """The shared «بازگشت» on a row of its own, like every other screen of the bot."""
    return [kb.back_button(current_ui_snapshot(), callback_data)]


def question_markup(*, has_back: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✅ بله، دکمه بگذار", callback_data=CB_YES)],
        [InlineKeyboardButton(text="➡️ ارسال بدون دکمه", callback_data=CB_NO)],
    ]
    if has_back:
        rows.append(back_row(CB_BACK))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def option_labels(*, renew: bool, labels: ScreenLabels | None, links: dict[str, dict]) -> dict[str, str]:
    """Checklist key -> text: the bot screens (menu labels), then each link button added in this flow."""
    out = dict(target_options(renew=renew, links=False, labels=labels))
    out.update({key: f"{'💎' if link.get('target') == TARGET_PLAN else '🔗'} {link.get('label', '')}" for key, link in links.items()})
    return out


def option_icons(labels: ScreenLabels | None, links: dict[str, dict] | None = None) -> dict[str, str]:
    """Checklist key -> premium emoji id: a screen's menu label, or the one typed with a link button's text."""
    icons = {key: label.icon for key, label in (labels or {}).items() if label.icon}
    icons.update({key: str(link["icon"]) for key, link in (links or {}).items() if link.get("icon")})
    return icons


def link_label_from_message(message: Message) -> tuple[str, str]:
    """(text, premium emoji id) of a typed button label; the premium emoji becomes the button's icon.

    A label made only of a premium emoji keeps the emoji's own fallback character as its text.
    """
    raw = message.text or ""
    packed = pack_rich_text(raw, message.entities or None)
    icon = button_icon_custom_emoji_id(packed) or ""
    text = button_display_text(packed).strip() if icon else raw.strip()
    return (text, icon) if text else (raw.strip(), "")


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
    back: str | None = None,
) -> InlineKeyboardMarkup:
    """Two columns of ✅/⬜ targets and added links, «لینک دلخواه», one row per saved set, «ادامه», then «بازگشت»."""
    icons = option_icons(labels, links)
    options = [
        InlineKeyboardButton(
            text=f"{RTL_MARK}{label} {'✅' if key in selected else '⬜'}", callback_data=f"{CB_TOGGLE}{key}",
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
    if back:
        rows.append(back_row(back))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def next_style(style: str) -> str:
    """The colour after ``style`` in the web form's order; an unknown value starts over at white."""
    current = normalize_style(style)
    return STYLE_CYCLE[(STYLE_CYCLE.index(current) + 1) % len(STYLE_CYCLE)]


def colors_markup(
    selected: list[str], styles: dict[str, str], *, labels: dict[str, str], icons: dict[str, str] | None = None,
    send_label: str = SEND_LABEL,
) -> InlineKeyboardMarkup:
    """Each chosen button drawn in its colour and premium emoji (a tap changes the colour), «ارسال», then «بازگشت»."""
    rows = []
    for key in selected:
        style = normalize_style(styles.get(key))
        extra = {"style": style} if style else {}
        text = f"{labels.get(key, key)} · {STYLE_NAMES[style]}"
        rows.append([InlineKeyboardButton(
            text=text, callback_data=f"{CB_COLOR}{key}", **extra, **_icon_extra((icons or {}).get(key))
        )])
    rows.append([InlineKeyboardButton(text=send_label, callback_data=CB_SEND)])
    rows.append(back_row(CB_YES))
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
    """Ask the question in place of the text prompt (the same panel); the flow's own data stays in ``state``.

    The text prompt this message answered is remembered, so the question's «بازگشت» asks it again.
    """
    text_step = await _text_step(state)
    await finish_text_step(message, state, text=QUESTION, inline=question_markup(has_back=text_step is not None))
    await state.set_state(PickerStates.picking)
    await state.update_data(
        mbp_flow=flow, mbp_shop=shop_id, mbp_renew=renew, mbp_selected=[], mbp_styles={}, mbp_links={},
        mbp_entry=ENTRY_QUESTION, mbp_text_step=text_step,
    )


async def _text_step(state: FSMContext) -> dict[str, str | None] | None:
    """The prompt the flow is in (state, text, cancel code, return), or ``None`` when it cannot be asked again."""
    current = await state.get_state()
    data = await state.get_data()
    prompt, cancel = data.get(PROMPT_TEXT_KEY), data.get(CANCEL_CODE_KEY)
    if not current or not prompt or not cancel:
        return None
    return {"state": current, "prompt": str(prompt), "cancel": str(cancel), "return_to": data.get(RETURN_TO_KEY)}


def initial_choice(buttons: list[ButtonInput]) -> tuple[list[str], dict[str, str], dict[str, dict]]:
    """Saved buttons as the checklist's (selected keys, colours, link and plan entries)."""
    selected: list[str] = []
    styles: dict[str, str] = {}
    links: dict[str, dict] = {}
    for item in buttons[:BUTTONS_MAX]:
        if item.target in SCREENS:
            key = item.target
        elif item.target in (TARGET_LINK, TARGET_PLAN):
            key = new_link_key(links)
            links[key] = {"target": item.target, "label": item.label, "url": item.url, "icon": item.icon, "plan": item.plan}
        else:
            continue
        if key not in selected:
            selected.append(key)
            styles[key] = normalize_style(item.style)
    return selected, styles, links


async def open_checklist(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, *, flow: str, shop_id: int | None,
    initial: list[ButtonInput], flow_data: dict | None = None,
) -> None:
    """Open the checklist directly, filled with ``initial`` (editing saved buttons); «بازگشت» runs the flow's ``back``."""
    selected, styles, links = initial_choice(initial)
    await state.set_state(PickerStates.picking)
    await state.update_data(
        mbp_flow=flow, mbp_shop=shop_id, mbp_renew=False, mbp_selected=selected, mbp_styles=styles, mbp_links=links,
        mbp_entry=ENTRY_CHECKLIST, mbp_text_step=None, **(flow_data or {}),
    )
    await _show_checklist(callback, state, session)


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
        back=CB_QUESTION if data.get("mbp_entry") != ENTRY_CHECKLIST else CB_BACK,
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
    icons = option_icons(await sender_screen_labels(session, data.get("mbp_shop")), dict(data.get("mbp_links") or {}))
    markup = colors_markup(
        list(data.get("mbp_selected") or []), dict(data.get("mbp_styles") or {}),
        labels=await _labels(state, session), icons=icons,
        send_label=_SEND_LABELS.get(str(data.get("mbp_flow") or ""), SEND_LABEL),
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
        if link is not None and link.get("target") == TARGET_PLAN:
            out.append(ButtonInput(TARGET_PLAN, str(link.get("label") or ""), "", style, str(link.get("plan") or "")))
        elif link is not None:
            out.append(ButtonInput(
                TARGET_LINK, str(link.get("label") or ""), str(link.get("url") or ""), style, icon=str(link.get("icon") or "")
            ))
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
        await callback.message.edit_text(LINK_ASK_LABEL, reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_row(CB_YES)]))


@router.message(PickerStates.link_label)
async def on_link_label(message: Message, state: FSMContext) -> None:
    label, icon = link_label_from_message(message)
    try:
        # Checked with a placeholder link; the real one is checked in the next step.
        make_button(ButtonInput(TARGET_LINK, label, "https://example.com"), ButtonRules(allow_links=True))
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}")
        return
    if not label:
        await message.answer(LINK_ASK_LABEL)
        return
    await state.update_data(mbp_link_label=label, mbp_link_icon=icon)
    await state.set_state(PickerStates.link_url)
    await message.answer(LINK_ASK_URL, reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_row(CB_LINK_BACK)]))


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
        links[key] = {
            "label": str(data.get("mbp_link_label") or ""), "url": url, "icon": str(data.get("mbp_link_icon") or ""),
        }
        selected.append(key)
    await state.update_data(mbp_links=links, mbp_selected=selected, mbp_link_label=None, mbp_link_icon=None)
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


@router.callback_query(F.data == CB_LINK_BACK, PickerStates.link_url)
async def on_link_url_back(callback: CallbackQuery, state: FSMContext) -> None:
    """From the link step back to the link's text."""
    await state.set_state(PickerStates.link_label)
    await state.update_data(mbp_link_label=None, mbp_link_icon=None)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(LINK_ASK_LABEL, reply_markup=InlineKeyboardMarkup(inline_keyboard=[back_row(CB_YES)]))


@router.callback_query(F.data == CB_QUESTION, PickerStates.picking)
async def on_question(callback: CallbackQuery, state: FSMContext) -> None:
    """From the checklist back to «دکمه پایین پیام باشه؟»."""
    data = await state.get_data()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(QUESTION, reply_markup=question_markup(has_back=bool(data.get("mbp_text_step"))))


@router.callback_query(F.data == CB_BACK, PickerStates.picking)
async def on_back(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, **kwargs) -> None:
    """The first screen's «بازگشت»: the flow's own screen, or the text prompt asked again as it was."""
    data = await state.get_data()
    flow = str(data.get("mbp_flow") or "")
    step = data.get("mbp_text_step")
    back = _BACKS.get(flow) if data.get("mbp_entry") == ENTRY_CHECKLIST else None
    if back is None and not step:
        await callback.answer(EXPIRED, show_alert=True)
        return
    await callback.answer()
    await state.update_data(mbp_selected=[], mbp_links={}, mbp_styles={}, mbp_text_step=None)
    if back is not None:
        await state.set_state(None)
        await back(callback, state, session, db_user, **_extra(kwargs))
        return
    await ask_text(
        callback, state, prompt=str(step["prompt"]), cancel_code=str(step["cancel"]), fsm_state=str(step["state"]),
        return_to=step.get("return_to"), edit=True,
    )


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
