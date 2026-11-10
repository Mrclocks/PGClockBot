"""Customers who took the free trial, with the buyers marked (web «کاربران تست»), and a message to them.

Listing needs ``trial_users``. Messaging needs ``trial_users_manage``, asks for the text, shows the
audience size and queues the campaign only after a confirmation. Blocking stays on the customer card.
"""
from __future__ import annotations

import html
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feature_staff import resolve_feature_staff
from app.bot.nav_inline import with_inline_back
from app.bot.nav_input import CancelEntry, ask_text, finish_text_step, register_cancel_code, try_legacy_cancel
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser
from app.services.user_errors import domain_error_text
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_toman, info_block, kv_line
from app.services.campaigns import clean_campaign_text
from app.services.shop_scope import ShopScopeError, is_platform_admin, resolve_shop_scope_id
from app.services.table_bulk import MAX_BULK
from app.services.trial_user_actions import message_trial_users
from app.services.trial_users import FILTER_LABELS, list_trial_users, normalize_filter, trial_summary
from app.services.users import get_all_settings

router = Router(name="trial_users_view")

PERMISSION = "trial_users"
MANAGE_PERMISSION = "trial_users_manage"
MESSAGE_CANCEL_CODE = "tru_msg"
TEXT_PREVIEW = 300
REQUEST_KEY_PREFIX = "tru:"
PAGE_SIZE = 8
PLATFORM_BACK = "nv:adm:marketing"
SHOP_BACK = "nv:res:home"


class TrialMessageStates(StatesGroup):
    text = State()


def _customer_callback(staff: dict, user_id: int) -> str:
    return f"adm:users:view:{user_id}" if is_platform_admin(staff) else f"res:user:{user_id}"


@router.callback_query(F.data.regexp(r"^tru:l:(all|buyers|non_buyers):(\d{1,12})$"))
async def show_trial_users(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await resolve_feature_staff(
        session, db_user, PERMISSION, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    try:
        if staff is None:
            raise ShopScopeError(feedback.NO_ACCESS)
        shop_id = resolve_shop_scope_id(staff)
    except ShopScopeError:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    _, _, raw_filter, raw_before = callback.data.split(":")
    trial_filter = normalize_filter(raw_filter)
    page = await list_trial_users(
        session, staff=staff, shop_id=shop_id, trial_filter=trial_filter,
        before=int(raw_before) or None, limit=PAGE_SIZE,
    )
    summary = await trial_summary(session, shop_id=shop_id)
    currency = get_settings().currency
    lines = [
        kv_line("🧪", "کل تست‌ها", str(summary.trials)),
        kv_line("🛒", "خریدار شده", str(summary.buyers)),
        kv_line("🔎", "فیلتر", FILTER_LABELS[trial_filter]),
    ]
    body = [info_block(lines), ""]
    rows: list[list[InlineKeyboardButton]] = []
    for row in page.rows:
        bought = f"خریدار ({row.purchase_count}، {format_toman(row.total_spent, currency)})" if row.is_buyer else "بدون خرید"
        flags = " 🚫" if row.is_blocked else ""
        body.append(f"• <b>{html.escape(row.name)}</b>{flags} — {html.escape(row.trial_state_label)} — {bought}")
        rows.append([InlineKeyboardButton(
            text=f"👤 {row.name}"[:60], callback_data=_customer_callback(staff, row.user_id),
        )])
    if not page.rows:
        body.append("موردی پیدا نشد.")
    filters = [
        InlineKeyboardButton(text=("✓ " if key == trial_filter else "") + label, callback_data=f"tru:l:{key}:0")
        for key, label in FILTER_LABELS.items()
    ]
    rows.insert(0, filters)
    if page.next_before:
        rows.append([InlineKeyboardButton(text="بعدی ▶️", callback_data=f"tru:l:{trial_filter}:{page.next_before}")])
    if page.rows and await _can_manage(session, db_user, is_reseller_bot, reseller_owner_id):
        rows.append([InlineKeyboardButton(text="📣 پیام به این گروه", callback_data=f"tru:m:{trial_filter}")])
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, PLATFORM_BACK if is_platform_admin(staff) else SHOP_BACK)
    if callback.message:
        await safe_edit_text(callback.message, format_message("🧪 کاربران تست", "\n".join(body)), reply_markup=markup)


async def _can_manage(session: AsyncSession, db_user: BotUser, is_reseller_bot: bool, reseller_owner_id: int | None) -> bool:
    return await resolve_feature_staff(
        session, db_user, MANAGE_PERMISSION, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ) is not None


async def _manage_scope(
    session: AsyncSession, db_user: BotUser, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> tuple[dict, int | None] | None:
    staff = await resolve_feature_staff(
        session, db_user, MANAGE_PERMISSION, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if staff is None:
        return None
    try:
        return staff, resolve_shop_scope_id(staff)
    except ShopScopeError:
        return None


async def _audience_ids(session: AsyncSession, staff: dict, shop_id: int | None, trial_filter: str) -> list[int]:
    page = await list_trial_users(
        session, staff=staff, shop_id=shop_id, trial_filter=normalize_filter(trial_filter), limit=MAX_BULK
    )
    return [row.user_id for row in page.rows]


@router.callback_query(F.data.regexp(r"^tru:m:(all|buyers|non_buyers)$"))
async def ask_message(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    scope = await _manage_scope(session, db_user, is_reseller_bot, reseller_owner_id)
    if scope is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    _, shop_id = scope
    trial_filter = callback.data.split(":")[2]
    await callback.answer()
    await state.update_data(tru_filter=trial_filter, tru_shop=shop_id or 0, tru_ids=None, tru_text=None)
    await ask_text(
        callback, state,
        prompt=(
            f"📣 متن پیام را بنویسید. به «{FILTER_LABELS[trial_filter]}» (حداکثر {MAX_BULK} نفر) ارسال می‌شود؛ "
            "مشتریانی که پیام بازاریابی را خاموش کرده‌اند یا مسدودند دریافت نمی‌کنند."
        ),
        cancel_code=MESSAGE_CANCEL_CODE, fsm_state=TrialMessageStates.text,
        return_to=f"tru:l:{trial_filter}:0", edit=True,
    )


@router.message(TrialMessageStates.text)
async def receive_message_text(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    if await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    scope = await _manage_scope(session, db_user, is_reseller_bot, reseller_owner_id)
    data = await state.get_data()
    if scope is None or int(data.get("tru_shop") or 0) != int(scope[1] or 0):
        await state.clear()
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    try:
        text = clean_campaign_text(message.text)
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    staff, shop_id = scope
    ids = await _audience_ids(session, staff, shop_id, str(data.get("tru_filter") or "all"))
    if not ids:
        await state.clear()
        await message.answer("گیرنده‌ای پیدا نشد.")
        return
    await state.update_data(tru_ids=ids, tru_text=text, tru_token=secrets.token_hex(16))
    await state.set_state(None)
    preview = html.escape(text[:TEXT_PREVIEW])
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید و ارسال", callback_data="tru:ok")],
        [InlineKeyboardButton(text="✖️ انصراف", callback_data=f"tru:l:{data.get('tru_filter') or 'all'}:0")],
    ])
    await finish_text_step(
        message, state,
        text=format_message("📣 تأیید ارسال", f"به حداکثر {len(ids)} نفر ارسال می‌شود:\n\n{preview}"),
        inline=markup,
    )


@router.callback_query(F.data == "tru:ok")
async def confirm_message(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    await _send_confirmed(callback, state, session, db_user, is_reseller_bot, reseller_owner_id)


async def _send_confirmed(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool, reseller_owner_id: int | None,
) -> None:
    scope = await _manage_scope(session, db_user, is_reseller_bot, reseller_owner_id)
    data = await state.get_data()
    ids, text, token = data.get("tru_ids"), data.get("tru_text"), data.get("tru_token")
    if scope is None or not ids or not text or not token or int(data.get("tru_shop") or 0) != int(scope[1] or 0):
        await state.update_data(tru_ids=None, tru_text=None, tru_token=None)
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await state.update_data(tru_ids=None, tru_text=None, tru_token=None)
    staff, shop_id = scope
    try:
        # The token is unique to this confirmation: a repeated or raced tap reuses the one campaign.
        campaign = await message_trial_users(
            session, staff=staff, shop_id=shop_id, user_ids=list(ids), text=str(text),
            request_key=f"{REQUEST_KEY_PREFIX}{token}",
        )
    except ValueError as exc:
        await session.rollback()
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer(f"پیام در صف ارسال قرار گرفت (کمپین #{campaign.id}) ✅", show_alert=True)
    if callback.message:
        ui = await get_all_settings(session)
        back = with_inline_back(None, ui, f"tru:l:{data.get('tru_filter') or 'all'}:0")
        await safe_edit_text(callback.message, format_message("📣 ارسال", "پیام در صف ارسال قرار گرفت."), reply_markup=back)


async def _reopen_after_cancel(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext, **_kw) -> None:
    from app.bot.nav_inline import present_nav_panel

    await state.clear()
    await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)


register_cancel_code(MESSAGE_CANCEL_CODE, CancelEntry(reopen=_reopen_after_cancel))
