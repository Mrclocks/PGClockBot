"""Owner bot: a reseller package's PasarGuard access switch and PAYG options.

Opened from the package screen of ``admin_plans``; every change goes through
``reseller_plan_payg`` so the bot and the web form save the same validated values.
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import is_platform_admin, require_bot_owner_handler
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, ResellerPlan
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_toman
from app.services.payg_pricing import BASIS_TRAFFIC, BASIS_USER, BASIS_USER_TRAFFIC
from app.services.reseller_plan_payg import (
    PlanFlag,
    PlanPrice,
    is_payg_plan,
    parse_plan_price,
    set_plan_basis,
    set_plan_price,
    toggle_plan_flag,
)

router = Router(name="admin_resplan_payg")

PREFIX = "adm:rpg"
BASIS_LABELS = {BASIS_TRAFFIC: "حجم", BASIS_USER: "کاربر", BASIS_USER_TRAFFIC: "کاربر + حجم"}
PRICE_PROMPTS = {
    PlanPrice.USER: "قیمت هر کاربر را به تومان بفرستید.",
    PlanPrice.UNLIMITED: "قیمت کاربر نامحدود را به تومان بفرستید؛ «-» یعنی ساخت کاربر نامحدود ممنوع است.",
}


class PlanPriceInput(StatesGroup):
    value = State()


def _mark(on: bool) -> str:
    return "✅ " if on else "⬜️ "


def screen_text(plan: ResellerPlan) -> str:
    lines = [f"💳 <b>PAYG و دسترسی پاسارگارد</b>\nپکیج: {html.escape(str(plan.name or ''))}"]
    if is_payg_plan(plan):
        unlimited = plan.payg_unlimited_price
        lines += [
            f"مبنای هزینه: {BASIS_LABELS.get(str(plan.payg_basis or BASIS_TRAFFIC), 'حجم')}",
            f"قیمت هر کاربر: {format_toman(int(plan.payg_user_price or 0))}",
            "کاربر نامحدود: " + ("ممنوع" if unlimited is None else format_toman(int(unlimited))),
            "تغییرها فقط روی هزینه‌های بعدی اثر دارند.",
        ]
    return "\n".join(lines)


def screen_keyboard(plan: ResellerPlan) -> InlineKeyboardMarkup:
    pid = int(plan.id)
    access = plan.allow_pg_panel_access is not False
    rows = [[InlineKeyboardButton(text=_mark(access) + "دسترسی به پاسارگارد", callback_data=f"{PREFIX}:f:acc:{pid}")]]
    if is_payg_plan(plan):
        basis = str(plan.payg_basis or BASIS_TRAFFIC)
        rows.append([
            InlineKeyboardButton(text=("🔘 " if basis == code else "") + label, callback_data=f"{PREFIX}:b:{code}:{pid}")
            for code, label in BASIS_LABELS.items()
        ])
        rows += [
            [InlineKeyboardButton(text=_mark(plan.payg_charge_upfront is True) + "کسر حجم همان لحظهٔ ساخت",
                                  callback_data=f"{PREFIX}:f:up:{pid}")],
            [InlineKeyboardButton(text=_mark(plan.payg_user_price_per_30d is True) + "قیمت کاربر به ازای هر ۳۰ روز",
                                  callback_data=f"{PREFIX}:f:p30:{pid}")],
            [InlineKeyboardButton(text="✏️ قیمت هر کاربر", callback_data=f"{PREFIX}:p:user:{pid}")],
            [InlineKeyboardButton(text="✏️ قیمت کاربر نامحدود", callback_data=f"{PREFIX}:p:unl:{pid}")],
        ]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"adm:resplan:view:{pid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: CallbackQuery, plan: ResellerPlan) -> None:
    if callback.message:
        await safe_edit_text(callback.message, screen_text(plan), reply_markup=screen_keyboard(plan))


async def _guard(callback: CallbackQuery, db_user: BotUser) -> bool:
    if is_platform_admin(db_user):
        return True
    await callback.answer(feedback.NOT_ADMIN, show_alert=True)
    return False


@router.callback_query(F.data.regexp(rf"^{PREFIX}:\d+$"))
@require_bot_owner_handler
async def payg_screen(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    if not await _guard(callback, db_user):
        return
    plan = await session.get(ResellerPlan, int(callback.data.rsplit(":", 1)[-1]))
    if plan is None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _show(callback, plan)


@router.callback_query(F.data.regexp(rf"^{PREFIX}:f:(acc|up|p30):\d+$"))
@require_bot_owner_handler
async def payg_flag(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    if not await _guard(callback, db_user):
        return
    _, _, _, flag, pid = callback.data.split(":")
    try:
        plan = await toggle_plan_flag(session, int(pid), PlanFlag(flag))
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(feedback.UPDATED)
    await _show(callback, plan)


@router.callback_query(F.data.regexp(rf"^{PREFIX}:b:(traffic|user|user_traffic):\d+$"))
@require_bot_owner_handler
async def payg_basis(callback: CallbackQuery, session: AsyncSession, db_user: BotUser) -> None:
    if not await _guard(callback, db_user):
        return
    _, _, _, basis, pid = callback.data.split(":")
    try:
        plan = await set_plan_basis(session, int(pid), basis)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(feedback.UPDATED)
    await _show(callback, plan)


@router.callback_query(F.data.regexp(rf"^{PREFIX}:p:(user|unl):\d+$"))
@require_bot_owner_handler
async def payg_price_ask(callback: CallbackQuery, state: FSMContext, db_user: BotUser) -> None:
    if not await _guard(callback, db_user):
        return
    _, _, _, price, pid = callback.data.split(":")
    await state.set_state(PlanPriceInput.value)
    await state.update_data(rpg_plan_id=int(pid), rpg_price=price)
    await callback.answer()
    if callback.message:
        await callback.message.answer(PRICE_PROMPTS[PlanPrice(price)])


@router.message(PlanPriceInput.value)
@require_bot_owner_handler
async def payg_price_save(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser) -> None:
    if not is_platform_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    price = PlanPrice(str(data.get("rpg_price")))
    try:
        plan = await set_plan_price(session, int(data.get("rpg_plan_id") or 0), price,
                                    parse_plan_price(message.text, price))
    except ValueError as exc:
        await message.answer(html.escape(str(exc)))
        return
    await state.clear()
    await message.answer(screen_text(plan), reply_markup=screen_keyboard(plan))
