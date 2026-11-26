"""Owner-only buttons around «وقتی استارت زد خبرم کن» for making someone a reseller.

``stw:add:<i|u>:<value>`` opens a watch for the tapping owner; ``stw:mk:<bot user id>`` (from the
arrival message) continues to the usual reseller-package step for that person. Registered behind
the owner-principal gate, and every handler also checks the representative-management right.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner_handler, require_platform_rep_mgmt
from app.bot.handlers.make_reseller_plan import ask_reseller_plan
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.start_watch import WATCH_DAYS, add_start_watch
from app.services.numbers import to_fa_digits
from app.services.telegram_target import TelegramTarget, parse_telegram_target

router = Router(name="start_watch")

WATCH_LABEL = "🔔 وقتی استارت زد خبرم کن"
WATCH_ADDED = "باشه؛ تا {days} روز، هر وقت ربات را استارت کند همین‌جا خبرتان می‌کنم."
WATCH_EXISTS = "از قبل منتظر همین شخص هستید."


def watch_offer_markup(target: TelegramTarget) -> InlineKeyboardMarkup:
    """The button under «not found» that opens a watch for exactly that id or username."""
    kind, value = ("i", target.telegram_id) if target.telegram_id is not None else ("u", target.username)
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=WATCH_LABEL, callback_data=f"stw:add:{kind}:{value}")]])


@router.callback_query(F.data.startswith("stw:add:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def start_watch_add(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    parts = (callback.data or "").split(":", 3)
    raw = parts[3] if len(parts) == 4 and parts[2] in {"i", "u"} else ""
    try:
        target = parse_telegram_target(raw)
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    added = await add_start_watch(session, watcher_telegram_id=callback.from_user.id, target=target)
    await callback.answer(WATCH_ADDED.format(days=to_fa_digits(WATCH_DAYS)) if added else WATCH_EXISTS, show_alert=True)


@router.callback_query(F.data.startswith("stw:mk:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def start_watch_make_reseller(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    raw = (callback.data or "").removeprefix("stw:mk:")
    user = await session.get(BotUser, int(raw)) if raw.isdigit() and len(raw) <= 10 else None
    if user is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await ask_reseller_plan(callback.message, state, session, user_id=int(user.id), can_approve=False)
