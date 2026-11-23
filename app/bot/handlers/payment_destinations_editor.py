"""Bot editor of the card, link-gateway and crypto-wallet lists (owner and shop settings).

``owner_router`` serves the platform owner on the main bot, behind the owner-principal
middleware; ``shop_router`` serves a shop owner for their own shop only. The rules live in
``app.services.payment_destination_edits``; these handlers parse, check the actor, call it and
render. Callback data: ``pde:<a|r>:<kind>:<view>[:<item id>[:<arg>]]``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.handlers.button_colors import shop_settings_shop_id
from app.bot.nav_input import ask_text, delete_user_message, finish_text_step
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services import payment_destination_edits as edits
from app.services.payment_destinations import MAX_ITEMS
from app.services.user_errors import domain_error_text

owner_router = Router(name="payment_destinations_owner")
shop_router = Router(name="payment_destinations_shop")

ROOT = "pde"
# Settings subsection (same ids in the owner and shop settings) that each list belongs to.
SETTINGS_SUB = {"c": "card", "g": "gateway", "w": "crypto"}
_SETTINGS_PREFIX = {"a": "adm:st:sub:pay", "r": "res:st:sub:pay"}
_CANCEL_CODES = {"a": "adm_set", "r": "rs_set"}
_STATE_KEY = "pde_new"
DELETE_CONFIRM = "⚠️ «{label}» از فهرست حذف شود؟ مشتری‌ها دیگر آن را نمی‌بینند."


class OwnerDestinationInput(StatesGroup):
    value = State()


class ShopDestinationInput(StatesGroup):
    value = State()


@dataclass(frozen=True, slots=True)
class Actor:
    """Who edits: the platform (``shop_id`` None) or one shop."""

    audience: str
    shop_id: int | None

    def data(self, kind: str, view: str, *args: object) -> str:
        return ":".join([ROOT, self.audience, kind, view, *(str(a) for a in args)])

    @property
    def input_state(self) -> State:
        return ShopDestinationInput.value if self.audience == "r" else OwnerDestinationInput.value


def entry_button(kind: str, *, for_reseller: bool) -> InlineKeyboardButton:
    """Button placed in the card, link-gateway or crypto settings subsection."""
    title = edits.KINDS[kind].title
    return InlineKeyboardButton(text=f"📋 {title}", callback_data=f"{ROOT}:{'r' if for_reseller else 'a'}:{kind}:l")


def destination_list_rows(sec_id: str, sub_id: str, *, for_reseller: bool) -> list[list[InlineKeyboardButton]]:
    """The list-editor row for the settings subsection that owns a destination list, else none."""
    if sec_id != "pay":
        return []
    kind = next((code for code, sub in SETTINGS_SUB.items() if sub == sub_id), None)
    return [[entry_button(kind, for_reseller=for_reseller)]] if kind else []


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=data)


def list_view(actor: Actor, kind: str, items: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    label = edits.LABELS[kind]
    rows = [
        [_button(f"{'' if item.get('enabled', True) else '⏸ '}{label(item)}", actor.data(kind, "i", item["id"]))]
        for item in items
    ]
    if len(items) < MAX_ITEMS:
        rows.append([_button("➕ افزودن", actor.data(kind, "n"))])
    rows.append([kb.back_button(None, f"{_SETTINGS_PREFIX[actor.audience]}:{SETTINGS_SUB[kind]}")])
    title = f"📋 <b>{edits.KINDS[kind].title}</b>"
    body = "برای ویرایش روی یک مورد بزنید. ترتیب همین فهرست، ترتیب نمایش به مشتری است." if items else "هنوز موردی ندارید."
    return f"{title}\n{body}", InlineKeyboardMarkup(inline_keyboard=rows)


def _details(kind: str, item: dict) -> str:
    fields = {"c": ("number", "holder"), "g": ("name", "link"), "w": ("asset", "network", "address")}[kind]
    names = {"number": "شماره", "holder": "صاحب کارت", "name": "نام", "link": "لینک", "asset": "ارز", "network": "شبکه", "address": "آدرس"}
    lines = [f"{names[f]}: <code>{html.escape(str(item.get(f) or '—'))}</code>" for f in fields]
    lines.append(f"وضعیت: {'فعال ✅' if item.get('enabled', True) else 'خاموش ⏸'}")
    return "\n".join(lines)


def item_view(actor: Actor, kind: str, item: dict) -> tuple[str, InlineKeyboardMarkup]:
    iid = item["id"]
    enabled = item.get("enabled", True)
    rows = [
        [_button("⏸ خاموش کن" if enabled else "▶️ روشن کن", actor.data(kind, "e", iid))],
        [_button("⬆️ بالاتر", actor.data(kind, "o", iid, "u")), _button("⬇️ پایین‌تر", actor.data(kind, "o", iid, "d"))],
        [_button("🗑 حذف", actor.data(kind, "x", iid))],
        [kb.back_button(None, actor.data(kind, "l"))],
    ]
    return f"📋 <b>{html.escape(edits.LABELS[kind](item))}</b>\n{_details(kind, item)}", InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_view(actor: Actor, kind: str, item: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [_button("🗑 بله، حذف شود", actor.data(kind, "X", item["id"]))],
        [kb.back_button(None, actor.data(kind, "i", item["id"]))],
    ]
    return DELETE_CONFIRM.format(label=html.escape(edits.LABELS[kind](item))), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    if callback.message:
        await safe_edit_text(callback.message, view[0], reply_markup=view[1])


def _item_or_list(actor: Actor, kind: str, items: list[dict], iid: str) -> tuple[str, InlineKeyboardMarkup]:
    item = edits.find_item(items, iid)
    return item_view(actor, kind, item) if item else list_view(actor, kind, items)


async def _ask_step(callback: CallbackQuery, state: FSMContext, actor: Actor, kind: str, step: int, values: dict) -> None:
    field, prompt = edits.KINDS[kind].steps[step]
    await state.update_data({_STATE_KEY: {"kind": kind, "step": step, "values": values}})
    await ask_text(
        callback,
        state,
        prompt=f"➕ <b>{edits.KINDS[kind].title}</b>\n{html.escape(prompt)}",
        cancel_code=_CANCEL_CODES[actor.audience],
        fsm_state=actor.input_state,
        edit=True,
        return_to=actor.data(kind, "l"),
    )


async def _change(session: AsyncSession, actor: Actor, kind: str, view: str, iid: str, arg: str) -> list[dict]:
    if view == "e":
        return await edits.toggle_item(session, kind, actor.shop_id, iid)
    if view == "o":
        return await edits.move_item(session, kind, actor.shop_id, iid, up=arg == "u")
    return await edits.delete_item(session, kind, actor.shop_id, iid)


async def handle_callback(callback: CallbackQuery, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) < 4 or parts[2] not in edits.KINDS:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    kind, view = parts[2], parts[3]
    iid = parts[4] if len(parts) > 4 else ""
    arg = parts[5] if len(parts) > 5 else ""
    try:
        items = await edits.list_items(session, kind, actor.shop_id)
        if view == "l":
            screen = list_view(actor, kind, items)
        elif view == "i":
            screen = _item_or_list(actor, kind, items, iid)
        elif view == "x":
            item = edits.find_item(items, iid)
            screen = confirm_view(actor, kind, item) if item else list_view(actor, kind, items)
        elif view in {"e", "o", "X"}:
            items = await _change(session, actor, kind, view, iid, arg)
            screen = list_view(actor, kind, items) if view == "X" else _item_or_list(actor, kind, items, iid)
        elif view == "n":
            if len(items) >= MAX_ITEMS:
                raise ValueError(edits.FULL_LIST)
            await callback.answer()
            await _ask_step(callback, state, actor, kind, 0, {})
            return
        else:
            await callback.answer(feedback.INVALID, show_alert=True)
            return
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer()
    await _show(callback, screen)


async def handle_message(message: Message, session: AsyncSession, state: FSMContext, actor: Actor) -> None:
    pending = (await state.get_data()).get(_STATE_KEY) or {}
    kind = pending.get("kind")
    text = (message.text or "").strip()
    if kind not in edits.KINDS:
        await state.set_state(None)
        return
    if kb.is_cancel_text(text):
        await delete_user_message(message)
        await state.set_state(None)
        view = list_view(actor, kind, await edits.list_items(session, kind, actor.shop_id))
        await finish_text_step(message, state, text=view[0], inline=view[1])
        return
    step = int(pending.get("step") or 0)
    steps = edits.KINDS[kind].steps
    field = steps[step][0]
    try:
        values = {**(pending.get("values") or {}), field: edits.clean_field(kind, field, text)}
        if step + 1 < len(steps):
            await state.update_data({_STATE_KEY: {"kind": kind, "step": step + 1, "values": values}})
            await message.answer(html.escape(steps[step + 1][1]))
            return
        items = await edits.add_item(session, kind, actor.shop_id, values)
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    await state.set_state(None)
    await state.update_data({_STATE_KEY: None})
    view = list_view(actor, kind, items)
    await finish_text_step(message, state, text=f"{feedback.SAVED_WITH_ICON}\n{view[0]}", inline=view[1])


# --- Owner (platform destinations, main bot) ------------------------------------------------
_OWNER = Actor(audience="a", shop_id=None)


@owner_router.callback_query(F.data.startswith(f"{ROOT}:a:"))
async def owner_destinations(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    # The owner-principal middleware on this router already admitted the actor.
    await handle_callback(callback, session, state, _OWNER)


@owner_router.message(OwnerDestinationInput.value)
async def owner_destination_input(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await handle_message(message, session, state, _OWNER)


# --- Shop (each shop's own destinations) ------------------------------------------------------
async def _shop_actor(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> Actor | None:
    shop_id = await shop_settings_shop_id(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return Actor(audience="r", shop_id=shop_id) if shop_id else None


@shop_router.callback_query(F.data.startswith(f"{ROOT}:r:"))
async def shop_destinations(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await callback.answer(feedback.NO_SHOP_SETTINGS_ACCESS, show_alert=True)
        return
    await handle_callback(callback, session, state, actor)


@shop_router.message(ShopDestinationInput.value)
async def shop_destination_input(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    actor = await _shop_actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        await state.clear()
        await message.answer(feedback.NO_SHOP_SETTINGS_ACCESS)
        return
    await handle_message(message, session, state, actor)
