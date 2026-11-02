"""«🧾 سفارش‌های من»: a customer follows their own orders from the wallet history screen.

The list and the card are scoped by the customer and by the shop whose bot is open, and say what to
do next (send the receipt, wait for review, contact support with the order number).
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, OrderStatus
from app.services import bot_feedback_texts as feedback
from app.services.customer_orders import CustomerOrder, get_customer_order, list_customer_orders
from app.services.formatting import format_toman
from app.services.users import current_shop_reseller_id, get_all_settings

router = Router(name="customer_orders")

LIST_CALLBACK = "myord:l"
BACK_CALLBACK = "nv:w:tx"
SUPPORT_CALLBACK = "nv:s:new"
ID = r"(\d{1,12})"
NO_ORDERS = "هنوز سفارشی ثبت نشده است."
NO_SUPPORT_STATUSES = (OrderStatus.DELIVERED.value, OrderStatus.CANCELLED.value)


def orders_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🧾 سفارش‌های من", callback_data=LIST_CALLBACK)


def with_orders_button(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """The wallet history keyboard plus the orders button, placed above its last (back) row."""
    rows = [list(row) for row in markup.inline_keyboard]
    rows.insert(max(len(rows) - 1, 0), [orders_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _line(item: CustomerOrder) -> str:
    plan = f" · {item.plan_name}" if item.plan_name else ""
    return f"#{item.order_id} · {item.kind}{plan} · {item.status_text}"[:64]


def _hint(item: CustomerOrder) -> str | None:
    if item.waiting_receipt:
        return "عکس رسید را در همین چت بفرستید؛ به آخرین پرداخت منتظر رسید شما پیوست می‌شود."
    if item.delayed:
        return "تحویل بیش از حد معمول طول کشیده است. با پشتیبانی تماس بگیرید و شناسه سفارش را بگویید."
    if item.status == OrderStatus.REJECTED.value:
        return "اگر فکر می‌کنید اشتباهی رخ داده، با پشتیبانی تماس بگیرید و شناسه سفارش را بگویید."
    if item.status == OrderStatus.DELIVERED.value:
        return "سرویس را در بخش «سرویس‌های من» می‌بینید."
    return None


async def _show(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), await get_all_settings(session), back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(rf"^myord:(l|m:{ID})$"))
async def orders_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    before = int(parts[2]) if len(parts) == 3 else None
    items, cursor = await list_customer_orders(session, db_user.id, shop_id=current_shop_reseller_id(), before_id=before)
    rows = [[InlineKeyboardButton(text=_line(item), callback_data=f"myord:v:{item.order_id}")] for item in items]
    if cursor is not None:
        rows.append([InlineKeyboardButton(text="سفارش‌های قدیمی‌تر ▶️", callback_data=f"myord:m:{cursor}")])
    text = "🧾 <b>سفارش‌های من</b>\nبرای دیدن وضعیت و راهنمایی، یکی را انتخاب کنید:" if rows else NO_ORDERS
    await _show(callback, session, text, rows, LIST_CALLBACK if before else BACK_CALLBACK)


@router.callback_query(F.data.regexp(rf"^myord:v:{ID}$"))
async def order_card(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    item = await get_customer_order(
        session, db_user.id, int(callback.data.split(":")[2]), shop_id=current_shop_reseller_id()
    )
    if item is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    lines = [
        f"🧾 <b>سفارش #{item.order_id}</b> — {item.kind}",
        f"پلن: {html.escape(item.plan_name) if item.plan_name else '—'}",
        f"مبلغ: {format_toman(item.amount, get_settings().currency)}",
        f"وضعیت: <b>{item.status_text}</b>",
    ]
    if item.created_at:
        lines.append(f"ثبت: {item.created_at:%Y-%m-%d %H:%M}")
    hint = _hint(item)
    if hint:
        lines.append(f"\n{hint}")
    rows = []
    if item.status not in NO_SUPPORT_STATUSES:
        rows.append([InlineKeyboardButton(text="✉️ تماس با پشتیبانی", callback_data=SUPPORT_CALLBACK)])
    await _show(callback, session, "\n".join(lines), rows, LIST_CALLBACK)
