"""Bot queue for automatic purchases parked for review: list -> card -> release.

Every step re-resolves the actor's shop and the ``orders`` permission; the service id in a callback
is only a lookup key inside that shop. A purchase whose order is unresolved cannot be released here.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.automation_review import (
    STATE_CLEARABLE,
    STATE_LABELS,
    ReviewItem,
    clear_review,
    list_review_queue,
)
from app.services.formatting import format_toman, order_status_fa
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.service_automation import LABELS
from app.services.users import get_all_settings

router = Router(name="automation_review")

PERMISSION = "orders"
PLATFORM_BACK = "adm:orders"
SHOP_BACK = "res:orders"
ID = r"(\d{1,12})"
NO_ITEMS = "خرید خودکار متوقف‌شده‌ای نیست."


async def review_scope(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> tuple[bool, int | None]:
    """(allowed, shop_id): the shop operator holding ``orders``, or the explicit platform Owner."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile:
        return has_bot_perm(profile, PERMISSION), owner_id
    if is_reseller_bot:
        return False, None
    return await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False), None


def review_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⚙️ خرید خودکار متوقف‌شده", callback_data="autorv:list")


async def _gate(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, **ctx) -> int | None | bool:
    """The caller's shop id (None = platform), or False after answering a denial."""
    allowed, shop_id = await review_scope(session, db_user, **ctx)
    if not allowed:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return False
    return shop_id


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


def _back(shop_id: int | None) -> str:
    return PLATFORM_BACK if shop_id is None else SHOP_BACK


async def _find(session: AsyncSession, shop_id: int | None, service_id: int) -> ReviewItem | None:
    """One parked item of the shop, found by paging the shop's own queue (the queue is short)."""
    cursor = max(0, service_id - 1)
    items, _ = await list_review_queue(session, shop_id=shop_id, after_service_id=cursor, limit=1)
    return items[0] if items and items[0].service_id == service_id else None


@router.callback_query(F.data == "autorv:list")
async def review_list(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    await callback.answer()
    items, _ = await list_review_queue(session, shop_id=shop_id, limit=15)
    rows = [
        [InlineKeyboardButton(text=f"{item.username} · {item.customer}"[:64], callback_data=f"autorv:v:{item.service_id}")]
        for item in items
    ]
    text = "⚙️ <b>خرید خودکار متوقف‌شده</b>\nیکی را برای بررسی انتخاب کنید:" if rows else NO_ITEMS
    await _edit(callback, session, text, rows, _back(shop_id))


async def _show_card(callback: CallbackQuery, session: AsyncSession, shop_id: int | None, service_id: int) -> None:
    item = await _find(session, shop_id, service_id)
    if item is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    lines = [
        f"⚙️ <b>{html.escape(item.username)}</b> — {html.escape(item.customer)}",
        f"عمل: {LABELS.get(item.action or '', 'نامشخص')}",
    ]
    if item.order_id:
        status = order_status_fa(item.order_status) if item.order_status else "پیدا نشد"
        lines.append(f"سفارش: #{item.order_id} ({status})")
    if item.amount is not None:
        lines.append(f"مبلغ: {format_toman(item.amount, get_settings().currency)}")
    if item.wallet_debited:
        lines.append("از کیف مشتری کسر شده است.")
    lines.append(f"\n{STATE_LABELS[item.state]}")
    rows: list[list[InlineKeyboardButton]] = []
    if item.state == STATE_CLEARABLE:
        if item.wallet_debited:
            rows.append([InlineKeyboardButton(text="✅ مبلغ برگشت داده شده؛ آزاد شود", callback_data=f"autorv:go:{service_id}:1")])
        else:
            rows.append([InlineKeyboardButton(text="✅ آزادسازی", callback_data=f"autorv:go:{service_id}:0")])
            rows.append([InlineKeyboardButton(text="🚫 آزاد و خاموش کردن خرید خودکار", callback_data=f"autorv:go:{service_id}:2")])
    await _edit(callback, session, "\n".join(lines), rows, "autorv:list")


@router.callback_query(F.data.regexp(rf"^autorv:v:{ID}$"))
async def review_card(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    await callback.answer()
    await _show_card(callback, session, shop_id, int(callback.data.split(":")[2]))


@router.callback_query(F.data.regexp(rf"^autorv:go:{ID}:[012]$"))
async def review_release(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    shop_id = await _gate(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if shop_id is False:
        return
    _, _, raw_id, mode = callback.data.split(":")
    service_id = int(raw_id)
    try:
        await clear_review(
            session, service_id, shop_id=shop_id, actor=f"tg:{db_user.telegram_id}",
            money_settled=mode == "1", disable_automation=mode == "2",
        )
    except ValueError as exc:
        await session.rollback()
        await callback.answer(str(exc)[:190], show_alert=True)
        await _show_card(callback, session, shop_id, service_id)
        return
    await callback.answer("آزاد شد", show_alert=True)
    items, _ = await list_review_queue(session, shop_id=shop_id, limit=15)
    rows = [
        [InlineKeyboardButton(text=f"{item.username} · {item.customer}"[:64], callback_data=f"autorv:v:{item.service_id}")]
        for item in items
    ]
    await _edit(callback, session, "✅ آزاد شد.\n" + ("" if rows else NO_ITEMS), rows, _back(shop_id))
