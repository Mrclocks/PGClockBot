"""Shop bot: risk tag, private note, shop wallet, loyalty points and shop block on a customer card.

Every action goes through the same service as the web panel, with the same permission key
(``customer_notes``, ``wallet_adjust``, ``loyalty_adjust``, ``customers_block``) and the same
checks: the shop comes from the bot the update arrived on, never from callback data; the
customer id in the callback is re-checked against that shop by the service on every step;
money and points changes carry a request key fixed when the flow starts, so a repeated tap or
message applies once.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.db.models import BotUser, ResellerProfile
from app.services import bot_feedback_texts as feedback
from app.services.authz import resolve_shop_permissions_from_profile
from app.services.color_tags import COLOR_TAGS, bot_tag_button_label
from app.services.customer_marks import has_customer_notes_permission, may_edit_private_marks, save_marks
from app.services.reseller_access import load_reseller_actor
from app.services.shop_customer_block import block_shop, set_shop_block, shop_block_state
from app.services.shop_manual_adjust import ManualAdjustError, new_request_key
from app.services.shop_points_adjust import adjust_shop_points, parse_points_request, points_adjust_shop
from app.services.shop_wallet_adjust import adjust_shop_wallet, parse_request, wallet_adjust_shop

logger = logging.getLogger(__name__)
router = Router(name="reseller_customer_tools")

PREFIX = "rct"
CANCEL = f"{PREFIX}:cancel"
KIND_WALLET = "wallet"
KIND_POINTS = "points"
DIRECTION_LABELS = {"credit": "افزایش", "debit": "کاهش"}


class CustomerToolStates(StatesGroup):
    note = State()
    amount = State()
    reason = State()
    block_reason = State()


def shop_bot_staff(profile: ResellerProfile, db_user: BotUser) -> dict:
    """The acting person as the services expect a shop staff member, scoped to this shop.

    The permissions are the shop's own; the label names who acted (owner or bot admin) for
    the staff action log.
    """
    handle = f"@{db_user.username}" if db_user.username else str(db_user.telegram_id)
    return {
        "role": "reseller",
        "bot_user_id": int(profile.user_id),
        "reseller_profile_id": int(profile.id),
        "permissions": list(resolve_shop_permissions_from_profile(profile) or []),
        "username": f"ربات: {handle}"[:64],
    }


def _allowed(check, staff: dict) -> bool:
    try:
        check(staff)
    except PermissionError:
        return False
    return True


def customer_tool_rows(profile: ResellerProfile, db_user: BotUser, user_id: int) -> list[list[InlineKeyboardButton]]:
    """Buttons for the customer card, only for the actions this shop's staff may take."""
    staff = shop_bot_staff(profile, db_user)
    uid = int(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    if has_customer_notes_permission(staff):
        rows.append([
            InlineKeyboardButton(text="🏷 تگ ریسک", callback_data=f"{PREFIX}:tag:{uid}"),
            InlineKeyboardButton(text="📝 یادداشت", callback_data=f"{PREFIX}:note:{uid}"),
        ])
    for kind, check, label in ((KIND_WALLET, wallet_adjust_shop, "💰 کیف پول"), (KIND_POINTS, points_adjust_shop, "⭐ امتیاز")):
        if _allowed(check, staff):
            rows.append([
                InlineKeyboardButton(text=f"{label} +", callback_data=f"{PREFIX}:{kind}:credit:{uid}"),
                InlineKeyboardButton(text=f"{label} −", callback_data=f"{PREFIX}:{kind}:debit:{uid}"),
            ])
    if _allowed(block_shop, staff):
        rows.append([InlineKeyboardButton(text="🚫 مسدودسازی در فروشگاه", callback_data=f"{PREFIX}:block:{uid}")])
    return rows


async def _staff_or_none(session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None) -> dict | None:
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        return None
    return shop_bot_staff(profile, db_user)


def _uid(data: str | None) -> int | None:
    tail = (data or "").rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() and len(tail) <= 12 else None


def _back_to_card(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, f"res:user:{int(user_id)}")]])


def _cancel_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ انصراف", callback_data=f"{CANCEL}:{int(user_id)}")]
    ])


async def _customer(session: AsyncSession, user_id: int | None) -> BotUser | None:
    return await session.get(BotUser, int(user_id)) if user_id else None


@router.callback_query(F.data.startswith(f"{CANCEL}:"))
async def rct_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("لغو شد")
    uid = _uid(callback.data)
    if callback.message and uid:
        await callback.message.edit_text("لغو شد.", reply_markup=_back_to_card(uid))


# ---- risk tag ----

@router.callback_query(F.data.startswith(f"{PREFIX}:tag:"))
async def rct_tag_menu(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
                       is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user = await _customer(session, _uid(callback.data))
    if staff is None or user is None or not await may_edit_private_marks(session, staff, user):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    rows = [[InlineKeyboardButton(text=bot_tag_button_label(t.key), callback_data=f"{PREFIX}:tagset:{t.key}:{user.id}")]
            for t in COLOR_TAGS]
    rows.append([kb.back_button(None, f"res:user:{user.id}")])
    if callback.message:
        await callback.message.edit_text("تگ ریسک این مشتری در فروشگاه شما:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith(f"{PREFIX}:tagset:"))
async def rct_tag_save(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
                       is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    parts = (callback.data or "").split(":")
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user = await _customer(session, _uid(callback.data))
    tag = parts[2] if len(parts) == 4 and parts[2] in {t.key for t in COLOR_TAGS} else None
    if staff is None or user is None or tag is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        await save_marks(session, staff=staff, user=user, tag=tag)
    except PermissionError:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await session.commit()
    await callback.answer("تگ ریسک ذخیره شد")
    if callback.message:
        await callback.message.edit_text(f"تگ ریسک: {bot_tag_button_label(tag)}", reply_markup=_back_to_card(int(user.id)))


# ---- private note ----

@router.callback_query(F.data.startswith(f"{PREFIX}:note:"))
async def rct_note_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
                         is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user = await _customer(session, _uid(callback.data))
    if staff is None or user is None or not await may_edit_private_marks(session, staff, user):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await state.set_state(CustomerToolStates.note)
    await state.update_data(rct_user_id=int(user.id))
    if callback.message:
        await callback.message.edit_text(
            "📝 یادداشت داخلی این مشتری را بفرستید (فقط کارکنان همین فروشگاه می‌بینند).\nبرای پاک کردن یک «-» بفرستید.",
            reply_markup=_cancel_markup(int(user.id)),
        )


@router.message(CustomerToolStates.note, F.text)
async def rct_note_save(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
                        is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    data = await state.get_data()
    await state.clear()
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user = await _customer(session, data.get("rct_user_id"))
    text = (message.text or "").strip()
    if staff is None or user is None:
        await message.answer(feedback.NO_ACCESS)
        return
    try:
        await save_marks(session, staff=staff, user=user, note="" if text == "-" else text)
    except PermissionError:
        await message.answer(feedback.NO_ACCESS)
        return
    await session.commit()
    await message.answer("یادداشت ذخیره شد.", reply_markup=_back_to_card(int(user.id)))


# ---- shop wallet and loyalty points ----

def _amount_prompt(kind: str, direction: str) -> str:
    what = "مبلغ (تومان)" if kind == KIND_WALLET else "تعداد امتیاز"
    target = "کیف پول این مشتری در فروشگاه شما" if kind == KIND_WALLET else "امتیاز این مشتری در فروشگاه شما"
    return f"{DIRECTION_LABELS[direction]} {target}\n\n{what} را به عدد بفرستید."


@router.callback_query(F.data.regexp(rf"^{PREFIX}:({KIND_WALLET}|{KIND_POINTS}):(credit|debit):\d+$"))
async def rct_balance_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
                            is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    _, kind, direction, _uid_text = (callback.data or "").split(":")
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    check = wallet_adjust_shop if kind == KIND_WALLET else points_adjust_shop
    user = await _customer(session, _uid(callback.data))
    if staff is None or user is None or not _allowed(check, staff):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await state.set_state(CustomerToolStates.amount)
    # Fixed for this flow: a repeated message or tap applies the change once.
    await state.update_data(rct_user_id=int(user.id), rct_kind=kind, rct_direction=direction, rct_key=new_request_key())
    if callback.message:
        await callback.message.edit_text(_amount_prompt(kind, direction), reply_markup=_cancel_markup(int(user.id)))


@router.message(CustomerToolStates.amount, F.text)
async def rct_balance_amount(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    if not raw.replace(",", "").isdigit():
        await message.answer("عدد صحیح مثبت بفرستید.", reply_markup=_cancel_markup(int(data.get("rct_user_id") or 0)))
        return
    await state.update_data(rct_amount=raw)
    await state.set_state(CustomerToolStates.reason)
    await message.answer("علت را بفرستید (حداقل ۳ کاراکتر؛ در گزارش کارکنان ثبت می‌شود).",
                         reply_markup=_cancel_markup(int(data.get("rct_user_id") or 0)))


@router.message(CustomerToolStates.reason, F.text)
async def rct_balance_apply(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
                            is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    data = await state.get_data()
    await state.clear()
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user_id = int(data.get("rct_user_id") or 0)
    if staff is None or not user_id:
        await message.answer(feedback.NO_ACCESS)
        return
    try:
        result = await _apply_balance_change(session, staff, data, user_id=user_id, reason=message.text)
    except PermissionError:
        await message.answer(feedback.NO_ACCESS)
        return
    except ManualAdjustError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=_back_to_card(user_id))
        return
    await message.answer(result, reply_markup=_back_to_card(user_id))


async def _apply_balance_change(session: AsyncSession, staff: dict, data: dict, *, user_id: int, reason: object) -> str:
    kind, direction = data.get("rct_kind"), data.get("rct_direction")
    if kind == KIND_WALLET:
        change = await adjust_shop_wallet(session, staff=staff, request=parse_request(
            customer_id=user_id, raw_amount=data.get("rct_amount"), direction=direction, reason=reason,
            request_key=data.get("rct_key"),
        ))
        return f"✅ کیف پول {change.amount:,} تومان {DIRECTION_LABELS[change.direction]} یافت · موجودی: {change.balance_after:,} تومان"
    change = await adjust_shop_points(session, staff=staff, request=parse_points_request(
        raw_customer_id=str(user_id), raw_points=data.get("rct_amount"), direction=direction, reason=reason,
        request_key=data.get("rct_key"),
    ))
    return f"✅ {change.points:,} امتیاز {DIRECTION_LABELS[change.direction]} یافت · مانده: {change.balance_after:,}"


# ---- shop block ----

@router.callback_query(F.data.startswith(f"{PREFIX}:block:"))
async def rct_block_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
                          is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    user = await _customer(session, _uid(callback.data))
    if staff is None or user is None or not _allowed(block_shop, staff):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    blocked, reason = await shop_block_state(session, int(user.id), int(staff["bot_user_id"]))
    if blocked:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ رفع مسدودی در فروشگاه", callback_data=f"{PREFIX}:unblock:{user.id}")],
            [kb.back_button(None, f"res:user:{user.id}")],
        ])
        text = "این مشتری در فروشگاه شما مسدود است." + (f"\nعلت: {html.escape(reason)}" if reason else "")
        if callback.message:
            await callback.message.edit_text(text, reply_markup=markup)
        return
    await state.set_state(CustomerToolStates.block_reason)
    await state.update_data(rct_user_id=int(user.id))
    if callback.message:
        await callback.message.edit_text(
            "🚫 علت مسدود کردن را بفرستید.\nفقط ربات و مینی‌اپ همین فروشگاه برای این مشتری بسته می‌شود؛ سرویس‌های فعلی‌اش قطع نمی‌شوند.",
            reply_markup=_cancel_markup(int(user.id)),
        )


@router.message(CustomerToolStates.block_reason, F.text)
async def rct_block_apply(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
                          is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    data = await state.get_data()
    await state.clear()
    await _set_block(message, session, db_user, user_id=int(data.get("rct_user_id") or 0), blocked=True,
                     reason=message.text, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)


@router.callback_query(F.data.startswith(f"{PREFIX}:unblock:"))
async def rct_unblock(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
                      is_reseller_bot: bool = False, reseller_owner_id: int | None = None) -> None:
    await callback.answer()
    if callback.message:
        await _set_block(callback.message, session, db_user, user_id=_uid(callback.data) or 0, blocked=False,
                         reason="", is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)


async def _set_block(message: Message, session: AsyncSession, db_user: BotUser, *, user_id: int, blocked: bool,
                     reason: object, is_reseller_bot: bool, reseller_owner_id: int | None) -> None:
    staff = await _staff_or_none(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if staff is None or not user_id:
        await message.answer(feedback.NO_ACCESS)
        return
    try:
        changed = await set_shop_block(session, staff=staff, customer_id=user_id, blocked=blocked, reason=reason)
    except PermissionError:
        await message.answer(feedback.NO_ACCESS)
        return
    except ManualAdjustError as exc:
        await message.answer(html.escape(str(exc)), reply_markup=_back_to_card(user_id))
        return
    if not changed:
        text = "وضعیت این مشتری از قبل همین بود."
    else:
        text = "🚫 مشتری در فروشگاه شما مسدود شد." if blocked else "✅ مسدودی مشتری در فروشگاه شما برداشته شد."
    await message.answer(text, reply_markup=_back_to_card(user_id))
