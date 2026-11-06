"""Owner-only: give a service whose selling shop is unknown its shop (web «تعیین فروشگاه سرویس»).

List -> service card with the evidence -> pick the platform or a shop -> confirm. The service layer
claims the row atomically, so a second tap is refused.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner
from app.bot.hub_entries import SERVICE_SHOP_REVIEW_CALLBACK
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, info_block, kv_line
from app.services.service_shop_review import (
    ReviewRow,
    assign_service_shop,
    list_assignable_shops,
    list_services_under_review,
)
from app.services.users import get_all_settings

router = Router(name="service_shop_review_view")

PAGE_SIZE = 8
LIST_CALLBACK = SERVICE_SHOP_REVIEW_CALLBACK
BACK_CALLBACK = "nv:adm:system"
PLATFORM_CHOICE = "p"
ID = r"(\d{1,12})"


async def _is_owner(session: AsyncSession, db_user: BotUser, is_reseller_bot: bool) -> bool:
    return not is_reseller_bot and await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False)


def _shop_text(shop_id: int | None, names: dict[int, str]) -> str:
    return "پلتفرم" if shop_id is None else html.escape(names.get(int(shop_id), f"#{shop_id}"))


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^ssr:l:(\d{1,4})$"))
async def show_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split(":")[2])
    rows_all = await list_services_under_review(session)
    if not rows_all:
        await _edit(callback, session, format_message("🏷 تعیین فروشگاه سرویس", "سرویسی در انتظار تعیین فروشگاه نیست. ✅"), [], BACK_CALLBACK)
        return
    last_page = (len(rows_all) - 1) // PAGE_SIZE
    page = min(page, last_page)
    rows = [
        [InlineKeyboardButton(text=f"#{r.service_id} · {r.pg_username}"[:64], callback_data=f"ssr:v:{r.service_id}")]
        for r in rows_all[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"ssr:l:{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"ssr:l:{page + 1}"))
    if nav:
        rows.append(nav)
    text = format_message(
        "🏷 تعیین فروشگاه سرویس",
        f"{len(rows_all)} سرویس تا تعیین فروشگاه از همهٔ فروشگاه‌ها پنهان است. یکی را انتخاب کنید.",
    )
    await _edit(callback, session, text, rows, BACK_CALLBACK)


async def _find(session: AsyncSession, service_id: int) -> ReviewRow | None:
    return next((r for r in await list_services_under_review(session) if r.service_id == service_id), None)


@router.callback_query(F.data.regexp(rf"^ssr:v:{ID}$"))
async def show_service(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    service_id = int(callback.data.split(":")[2])
    row = await _find(session, service_id)
    if row is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    shops = dict(await list_assignable_shops(session))
    evidence = "، ".join(_shop_text(s, shops) for s in row.order_shops) or "—"
    lines = [
        kv_line("📦", "سرویس", f"<code>{html.escape(row.pg_username)}</code> (#{row.service_id})"),
        kv_line("👤", "مشتری", f"#{row.customer_id}"),
        kv_line("🏬", "فروشگاه منتسب به مشتری", _shop_text(row.attributed_shop, shops)),
        kv_line("🧾", "فروشگاه سفارش‌های مشتری", evidence),
    ]
    rows = [[InlineKeyboardButton(text="🏠 پلتفرم (ربات اصلی)", callback_data=f"ssr:c:{service_id}:{PLATFORM_CHOICE}")]]
    rows += [
        [InlineKeyboardButton(text=f"🏬 {name}"[:64], callback_data=f"ssr:c:{service_id}:{shop_id}")]
        for shop_id, name in list(shops.items())[:PAGE_SIZE]
    ]
    rows.append([InlineKeyboardButton(text="👤 کارت مشتری", callback_data=f"adm:users:view:{row.customer_id}")])
    await _edit(callback, session, format_message("🏷 فروشگاه این سرویس کدام است؟", info_block(lines)), rows, LIST_CALLBACK)


@router.callback_query(F.data.regexp(rf"^ssr:c:{ID}:(p|\d{{1,12}})$"))
async def confirm_assign(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    _, _, raw_id, choice = callback.data.split(":")
    service_id = int(raw_id)
    if await _find(session, service_id) is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    shops = dict(await list_assignable_shops(session))
    shop_id = None if choice == PLATFORM_CHOICE else int(choice)
    if shop_id is not None and shop_id not in shops:
        await callback.answer("فروشگاه نامعتبر است", show_alert=True)
        return
    await callback.answer()
    text = format_message(
        f"⚠️ تأیید برای سرویس #{service_id}",
        f"سرویس به «{_shop_text(shop_id, shops)}» تعلق می‌گیرد و از این پس فقط همان فروشگاه آن را می‌بیند. این انتخاب یک‌بار انجام می‌شود.",
    )
    rows = [[InlineKeyboardButton(text="✅ تأیید و تعیین", callback_data=f"ssr:x:{service_id}:{choice}")]]
    await _edit(callback, session, text, rows, f"ssr:v:{service_id}")


@router.callback_query(F.data.regexp(rf"^ssr:x:{ID}:(p|\d{{1,12}})$"))
async def run_assign(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    _, _, raw_id, choice = callback.data.split(":")
    try:
        await assign_service_shop(session, int(raw_id), None if choice == PLATFORM_CHOICE else int(choice))
    except ValueError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await callback.answer("فروشگاه سرویس تعیین شد ✅", show_alert=True)
    remaining = await list_services_under_review(session)
    rows = [
        [InlineKeyboardButton(text=f"#{r.service_id} · {r.pg_username}"[:64], callback_data=f"ssr:v:{r.service_id}")]
        for r in remaining[:PAGE_SIZE]
    ]
    text = format_message("🏷 تعیین فروشگاه سرویس", f"{len(remaining)} سرویس دیگر در انتظار است." if remaining else "همهٔ سرویس‌ها تعیین تکلیف شدند. ✅")
    await _edit(callback, session, text, rows, BACK_CALLBACK)
