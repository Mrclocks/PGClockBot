"""Owner-only: recent broadcasts with their progress, and stopping one that is still queued or running."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner
from app.bot.broadcast_entries import BROADCAST_HISTORY_CALLBACK
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, BroadcastLog
from app.services import bot_feedback_texts as feedback
from app.services.broadcast import AUDIENCE_LABELS
from app.services.broadcast_queue import ACTIVE_STATUSES, STATUS_LABELS, cancel_broadcast
from app.services.formatting import format_message, format_number, info_block, kv_line
from app.services.users import get_all_settings

router = Router(name="broadcast_history")

LIST_LIMIT = 8
TEXT_PREVIEW = 300
BACK_CALLBACK = "nv:adm:broadcast"
ID = r"(\d{1,12})"


async def _is_owner(session: AsyncSession, db_user: BotUser, is_reseller_bot: bool) -> bool:
    return not is_reseller_bot and await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False)


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


def _label(log: BroadcastLog) -> str:
    return f"#{log.id} · {STATUS_LABELS.get(log.status, log.status)} · {format_number(log.ok_count)}/{format_number(log.total)}"


async def _render_list(callback: CallbackQuery, session: AsyncSession) -> None:
    logs = list(await session.scalars(select(BroadcastLog).order_by(BroadcastLog.id.desc()).limit(LIST_LIMIT)))
    if not logs:
        await _edit(callback, session, format_message("📜 تاریخچهٔ ارسال‌ها", "هنوز پیام گروهی ارسال نشده است."), [], BACK_CALLBACK)
        return
    rows = [[InlineKeyboardButton(text=_label(log)[:64], callback_data=f"bch:v:{log.id}")] for log in logs]
    await _edit(callback, session, format_message("📜 تاریخچهٔ ارسال‌ها", "یکی را برای دیدن پیشرفت یا توقف انتخاب کنید."), rows, BACK_CALLBACK)


async def _render_card(callback: CallbackQuery, session: AsyncSession, log: BroadcastLog) -> None:
    lines = [
        kv_line("🔢", "شناسه", f"#{log.id}"),
        kv_line("📌", "وضعیت", STATUS_LABELS.get(log.status, log.status)),
        kv_line("🎯", "مخاطبان", html.escape(AUDIENCE_LABELS.get(log.audience, log.audience))),
        kv_line("✅", "موفق", format_number(log.ok_count)),
        kv_line("❌", "ناموفق", format_number(log.fail_count)),
        kv_line("❓", "نتیجهٔ نامعلوم", format_number(log.review_count)),
        kv_line("👥", "کل", format_number(log.total)),
    ]
    body = f"{info_block(lines)}\n\n💬 <b>متن:</b>\n{html.escape((log.text or '')[:TEXT_PREVIEW])}"
    rows = []
    if log.status in ACTIVE_STATUSES:
        rows.append([InlineKeyboardButton(text="⛔ توقف ارسال", callback_data=f"bch:c:{log.id}")])
    await _edit(callback, session, format_message("📜 وضعیت ارسال", body), rows, BROADCAST_HISTORY_CALLBACK)


@router.callback_query(F.data == BROADCAST_HISTORY_CALLBACK)
async def show_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await _render_list(callback, session)


@router.callback_query(F.data.regexp(rf"^bch:v:{ID}$"))
async def show_card(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    log = await session.get(BroadcastLog, int(callback.data.split(":")[2]))
    if log is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _render_card(callback, session, log)


@router.callback_query(F.data.regexp(rf"^bch:c:{ID}$"))
async def confirm_cancel(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    log_id = int(callback.data.split(":")[2])
    log = await session.get(BroadcastLog, log_id)
    if log is None or log.status not in ACTIVE_STATUSES:
        await callback.answer("این ارسال دیگر فعال نیست", show_alert=True)
        return
    await callback.answer()
    text = format_message(
        f"⚠️ توقف ارسال #{log_id}",
        "ارسال متوقف می‌شود. پیام‌هایی که تا حالا فرستاده شده‌اند برنمی‌گردند.",
    )
    rows = [[InlineKeyboardButton(text="✅ توقف ارسال", callback_data=f"bch:x:{log_id}")]]
    await _edit(callback, session, text, rows, f"bch:v:{log_id}")


@router.callback_query(F.data.regexp(rf"^bch:x:{ID}$"))
async def run_cancel(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, is_reseller_bot: bool = False) -> None:
    if not await _is_owner(session, db_user, is_reseller_bot):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    log_id = int(callback.data.split(":")[2])
    if not await cancel_broadcast(session, log_id):
        await callback.answer("این ارسال دیگر فعال نیست", show_alert=True)
        return
    await callback.answer("ارسال متوقف شد؛ پیام‌های فرستاده‌شده برنمی‌گردند", show_alert=True)
    log = await session.get(BroadcastLog, log_id)
    await session.refresh(log)
    await _render_card(callback, session, log)
