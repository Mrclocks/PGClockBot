"""Owner-only: decide PAYG charges whose panel outcome was unknown (web «بازبینی هزینه‌های PAYG»).

List -> charge card -> «اعمال شده» (the charge stands) or «انجام نشده» (the money goes back to the
shop's wallet) -> confirm. ``payg_charges.resolve_review_charge`` claims the charge with an atomic
status update, so a second tap or a decision already taken on the web is refused.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner
from app.bot.nav_inline import with_inline_back
from app.bot.payg_review_entries import PAYG_REVIEW_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, ResellerCharge
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_number, format_toman, info_block, kv_line
from app.services.payg_charges import list_review_charges, resolve_review_charge
from app.services.user_errors import domain_error_text
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

router = Router(name="payg_charge_review")

LIST_CALLBACK = PAYG_REVIEW_CALLBACK
BACK_CALLBACK = "nv:adm:ops"
PAGE_SIZE = 8
ID = r"(\d{1,12})"
TITLE = "🧾 بازبینی هزینه‌های PAYG"
EVENT_LABELS: dict[str, str] = {
    "create": "ساخت",
    "renew": "تمدید",
    "add_volume": "افزایش حجم",
    "add_days": "افزایش روز",
    "reset": "ریست ترافیک",
    "reactivate": "فعال‌سازی دوباره",
}
SETTLED_MESSAGE = "هزینه تأیید شد ✅"
REFUNDED_MESSAGE = "مبلغ به کیف پول نماینده برگشت ✅"
ALREADY_DECIDED_MESSAGE = "این هزینه دیگر در انتظار بازبینی نیست"


async def _is_owner(session: AsyncSession, db_user: BotUser, is_reseller_bot: bool) -> bool:
    return not is_reseller_bot and await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False)


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


async def _shop_names(session: AsyncSession, shop_ids: set[int]) -> dict[int, str]:
    if not shop_ids:
        return {}
    users = (await session.scalars(select(BotUser).where(BotUser.id.in_(shop_ids)))).all()
    return {int(u.id): (u.full_name or u.username or f"#{int(u.id)}") for u in users}


def _event_text(charge: ResellerCharge) -> str:
    event = EVENT_LABELS.get(str(charge.event), str(charge.event))
    quantity = int(charge.quantity or 1)
    return f"{event} × {format_number(quantity)}" if quantity > 1 else event


def _row_label(charge: ResellerCharge, names: dict[int, str]) -> str:
    shop = names.get(int(charge.shop_id), f"#{charge.shop_id}")
    return f"{_event_text(charge)} · {shop} · {format_toman(int(charge.amount or 0))}"[:64]


async def _find(session: AsyncSession, charge_id: int) -> ResellerCharge | None:
    return next((c for c in await list_review_charges(session, shop_id=None) if int(c.id) == charge_id), None)


def _list_text(total: int) -> str:
    if not total:
        return format_message(TITLE, "هزینه‌ای در انتظار بازبینی نیست. ✅")
    return format_message(
        TITLE,
        f"پنل به {format_number(total)} تغییر پاسخ روشن نداد؛ مبلغ از کیف پول نماینده کم شده و نگه داشته شده است. "
        "یکی را انتخاب کنید.",
    )


async def _list_rows(session: AsyncSession, page: int) -> tuple[str, list[list[InlineKeyboardButton]]]:
    charges = await list_review_charges(session, shop_id=None)
    if not charges:
        return _list_text(0), []
    names = await _shop_names(session, {int(c.shop_id) for c in charges})
    last_page = (len(charges) - 1) // PAGE_SIZE
    page = min(max(page, 0), last_page)
    rows = [
        [InlineKeyboardButton(text=_row_label(c, names), callback_data=f"pcr:v:{int(c.id)}")]
        for c in charges[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"pcr:l:{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"pcr:l:{page + 1}"))
    if nav:
        rows.append(nav)
    return _list_text(len(charges)), rows


@router.callback_query(F.data.regexp(r"^pcr:l:(\d{1,3})$"))
async def show_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    text, rows = await _list_rows(session, int(callback.data.split(":")[2]))
    await _edit(callback, session, text, rows, BACK_CALLBACK)


def _card_text(charge: ResellerCharge, shop_name: str) -> str:
    created = charge.created_at.strftime("%Y-%m-%d %H:%M") if charge.created_at else "—"
    lines = [
        kv_line("🏬", "نماینده", html.escape(shop_name)),
        kv_line("🔁", "تغییر", html.escape(_event_text(charge))),
        kv_line("👤", "کاربر پنل", f"<code>{html.escape(charge.pg_username or '—')}</code>"),
        kv_line("🕒", "زمان", f"<code>{created}</code>"),
        kv_line("💰", "مبلغ", format_toman(int(charge.amount or 0))),
    ]
    return format_message(
        f"{TITLE} #{int(charge.id)}",
        f"{info_block(lines)}\n\nدر پنل بررسی کنید که این تغییر روی کاربر انجام شده است یا نه.",
    )


@router.callback_query(F.data.regexp(rf"^pcr:v:{ID}$"))
async def show_charge(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    charge = await _find(session, int(callback.data.split(":")[2]))
    if charge is None:
        await callback.answer(ALREADY_DECIDED_MESSAGE, show_alert=True)
        return
    await callback.answer()
    names = await _shop_names(session, {int(charge.shop_id)})
    rows = [
        [InlineKeyboardButton(text="✅ اعمال شده", callback_data=f"pcr:s:{int(charge.id)}")],
        [InlineKeyboardButton(text="↩️ انجام نشده، برگشت مبلغ", callback_data=f"pcr:r:{int(charge.id)}")],
    ]
    shop_name = names.get(int(charge.shop_id), f"#{charge.shop_id}")
    await _edit(callback, session, _card_text(charge, shop_name), rows, LIST_CALLBACK)


CONFIRM_TEXTS: dict[str, tuple[str, str, str]] = {
    # action -> (question, confirm button, callback prefix of the decision)
    "s": (
        "کاربر در پنل همان‌طور که خواسته شده بود تغییر کرده است؟ مبلغ کسرشده قطعی می‌شود.",
        "✅ بله، اعمال شده",
        "pcr:S",
    ),
    "r": (
        "تغییر در پنل انجام نشده است؟ مبلغ به کیف پول نماینده برمی‌گردد.",
        "↩️ بله، برگشت مبلغ",
        "pcr:R",
    ),
}


@router.callback_query(F.data.regexp(rf"^pcr:(s|r):{ID}$"))
async def confirm_decision(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    _, action, raw_id = callback.data.split(":")
    if await _find(session, int(raw_id)) is None:
        await callback.answer(ALREADY_DECIDED_MESSAGE, show_alert=True)
        return
    await callback.answer()
    question, button, prefix = CONFIRM_TEXTS[action]
    rows = [[InlineKeyboardButton(text=button, callback_data=f"{prefix}:{int(raw_id)}")]]
    await _edit(callback, session, format_message(f"⚠️ هزینهٔ #{int(raw_id)}", question), rows, f"pcr:v:{int(raw_id)}")


@router.callback_query(F.data.regexp(rf"^pcr:(S|R):{ID}$"))
async def run_decision(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    _, action, raw_id = callback.data.split(":")
    applied = action == "S"
    try:
        decided = await resolve_review_charge(session, int(raw_id), applied=applied, actor=f"ربات #{int(db_user.id)}")
    except ValueError as exc:
        await session.rollback()
        logger.warning("payg review decision failed charge=%s", raw_id, exc_info=True)
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    if not decided:
        await callback.answer(ALREADY_DECIDED_MESSAGE, show_alert=True)
    else:
        await callback.answer(SETTLED_MESSAGE if applied else REFUNDED_MESSAGE, show_alert=True)
    text, rows = await _list_rows(session, 0)
    await _edit(callback, session, text, rows, BACK_CALLBACK)
