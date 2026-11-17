"""Second step of «ساخت نماینده» in the bot: choose the reseller package.

A package carries the PasarGuard role, limits, billing mode and permissions. Without
one, a panel with several roles cannot tell which role the new admin should get.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import is_platform_admin, require_bot_owner_handler, require_platform_rep_mgmt
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.chat_records import keep_sent
from app.services.redact import user_safe_error
from app.services.resellers import (
    format_credentials_message,
    get_reseller_panel_base_url,
    grantable_reseller_plan,
    list_active_reseller_plans,
    provision_reseller,
)

logger = logging.getLogger(__name__)
router = Router(name="make_reseller_plan")

CALLBACK_PREFIX = "mkres:p:"
NO_PLAN = 0
BASIC_PERMS = "dashboard,orders,tickets,stats"
APPROVER_PERMS = "dashboard,orders,payments,tickets,stats"
PLAN_BUTTON_LIMIT = 40


class MakeResellerPlan(StatesGroup):
    choosing = State()


def _plan_keyboard(plans: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{'[PAYG]' if (p.billing_mode or 'fixed') == 'payg' else '[ثابت]'} {p.name}"[:64],
            callback_data=f"{CALLBACK_PREFIX}{int(p.id)}",
        )]
        for p in plans[:PLAN_BUTTON_LIMIT]
    ]
    rows.append([InlineKeyboardButton(text="بدون پلن", callback_data=f"{CALLBACK_PREFIX}{NO_PLAN}")])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="adm:resellers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_reseller_plan(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    user_id: int,
    can_approve: bool,
) -> None:
    """Remember the chosen customer and ask which package the new shop gets."""
    await state.set_state(MakeResellerPlan.choosing)
    await state.update_data(mkres_user_id=int(user_id), mkres_approve=bool(can_approve))
    plans = await list_active_reseller_plans(session)
    await message.answer(
        "پلن نمایندگی را انتخاب کنید.\n"
        "نقش پاسارگارد، سقف‌ها و دسترسی‌ها از همین پلن گرفته می‌شود. "
        "اگر پنل چند نقش دارد، انتخاب پلن لازم است.",
        reply_markup=_plan_keyboard(plans),
    )


def _parse_plan_id(data: str) -> int | None:
    raw = data.removeprefix(CALLBACK_PREFIX)
    return int(raw) if raw.isdigit() else None


@router.callback_query(MakeResellerPlan.choosing, F.data.startswith(CALLBACK_PREFIX))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def make_reseller_with_plan(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not is_platform_admin(db_user):
        await state.clear()
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan_id = _parse_plan_id(callback.data or "")
    data = await state.get_data()
    user = await session.get(BotUser, int(data.get("mkres_user_id") or 0))
    if plan_id is None or user is None:
        await state.clear()
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    can_approve = bool(data.get("mkres_approve"))
    perms = APPROVER_PERMS if can_approve else BASIC_PERMS
    try:
        plan = await grantable_reseller_plan(session, plan_id) if plan_id != NO_PLAN else None
        creds = await provision_reseller(
            session,
            user=user,
            plan=plan,
            can_approve_receipts=can_approve,
            web_permissions=None if plan is not None else perms,
            bot_permissions=None if plan is not None else perms,
            create_pg_admin=True,
            panel_base_url=await get_reseller_panel_base_url(session),
        )
    except Exception as exc:
        logger.warning("make reseller from bot failed user_id=%s", user.id, exc_info=True)
        await callback.answer()
        # A message, not an alert: the role hint is longer than an alert allows.
        if callback.message:
            await callback.message.answer(f"خطا: {html.escape(user_safe_error(exc))}")
        return
    await state.clear()
    await callback.answer()
    try:
        with keep_sent():  # a notice or review item, never a temporary panel
            await callback.bot.send_message(
                user.telegram_id, format_credentials_message(creds), parse_mode="HTML"
            )
    except Exception:
        logger.debug("make reseller: credentials message not delivered", exc_info=True)
    if callback.message:
        await callback.message.answer(
            f"کاربر {int(user.telegram_id)} نماینده شد ✅ — اطلاعات ورود ارسال شد"
        )
