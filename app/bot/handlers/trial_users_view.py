"""Read-only list of the customers who took the free trial, with the buyers marked (web «کاربران تست»)."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feature_staff import resolve_feature_staff
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_toman, info_block, kv_line
from app.services.shop_scope import ShopScopeError, is_platform_admin, resolve_shop_scope_id
from app.services.trial_users import FILTER_LABELS, list_trial_users, normalize_filter, trial_summary
from app.services.users import get_all_settings

router = Router(name="trial_users_view")

PERMISSION = "trial_users"
PAGE_SIZE = 8
PLATFORM_BACK = "nv:adm:marketing"
SHOP_BACK = "nv:res:home"


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
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, PLATFORM_BACK if is_platform_admin(staff) else SHOP_BACK)
    if callback.message:
        await safe_edit_text(callback.message, format_message("🧪 کاربران تست", "\n".join(body)), reply_markup=markup)
