"""Platform owner bot: increase or decrease a customer's platform loyalty points.

Amount then reason (min 3 chars); applies through ``admin_adjust_points`` with
``shop_id=None`` and an idempotent request key fixed when the flow starts.
"""

from __future__ import annotations

import html
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import is_platform_admin as _is_admin
from app.bot.auth import require_bot_owner_handler
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_number
from app.services.loyalty import admin_adjust_points, points_balance
from app.services.redact import user_safe_error
from app.services.shop_manual_adjust import ManualAdjustError, REASON_MIN, parse_reason

router = Router(name="admin_points_adjust")

POINTS_MAX = 1_000_000
DIRECTION_LABELS = {"credit": "افزایش", "debit": "کاهش"}


class PointsAdjustStates(StatesGroup):
    amount = State()
    reason = State()


def _parse_points_amount(raw: str) -> int:
    text = (raw or "").strip().replace(",", "").replace("٬", "")
    if not text.isdigit() or len(text) > 12:
        raise ValueError("تعداد امتیاز را به عدد صحیح مثبت بفرستید.")
    value = int(text)
    if not 1 <= value <= POINTS_MAX:
        raise ValueError(f"امتیاز باید بین ۱ و {POINTS_MAX:,} باشد.")
    return value


@router.callback_query(F.data.startswith("adm:users:pcredit:"))
@require_bot_owner_handler
async def adm_users_points_credit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    await _points_adjust_ask(callback, state, session, db_user, mode="credit")


@router.callback_query(F.data.startswith("adm:users:pdebit:"))
@require_bot_owner_handler
async def adm_users_points_debit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    await _points_adjust_ask(callback, state, session, db_user, mode="debit")


async def _points_adjust_ask(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    *,
    mode: str,
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    # Local import: admin is huge and this module must stay loadable without a cycle.
    from app.bot.handlers.admin import _deny_if_outside_platform_shop

    user_id = int(callback.data.split(":")[-1])
    user = await session.get(BotUser, user_id)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    if mode not in DIRECTION_LABELS:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    balance = await points_balance(session, user, None)
    await state.update_data(
        admin_points_user_id=user_id,
        admin_points_mode=mode,
        # One key per prompt: a repeated reason message applies the change once.
        admin_points_key=secrets.token_hex(8),
    )
    await callback.answer()
    from app.bot.nav_input import ask_text

    action = DIRECTION_LABELS[mode]
    prompt = (
        f"⭐ {action} امتیاز برای <b>{html.escape(user.full_name or str(user.telegram_id))}</b>\n"
        f"مانده فعلی: {format_number(balance)}\n\n"
        "تعداد امتیاز را به عدد مثبت بفرستید."
    )
    await ask_text(
        callback,
        state,
        prompt=prompt,
        cancel_code="adm_usr",
        return_to=f"adm:users:view:{user_id}",
        fsm_state=PointsAdjustStates.amount,
        edit=True,
    )


@router.message(PointsAdjustStates.amount)
@require_bot_owner_handler
async def adm_users_points_amount(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await state.clear()
        return
    if _is_cancel(message):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    data = await state.get_data()
    user_id = int(data.get("admin_points_user_id") or 0)
    mode = str(data.get("admin_points_mode") or "")
    # Local import to avoid a cycle with the large admin module.
    from app.bot.handlers.admin import _platform_shop_user

    user, deny = await _platform_shop_user(session, user_id) if user_id else (None, feedback.NOT_FOUND)
    if deny or user is None:
        await state.clear()
        await message.answer(deny or feedback.NOT_FOUND)
        return
    try:
        amount = _parse_points_amount(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(admin_points_amount=amount)
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt=(
            f"علت {DIRECTION_LABELS.get(mode, '')} {format_number(amount)} امتیاز را بفرستید "
            f"(حداقل {REASON_MIN} کاراکتر؛ در دفترکل ثبت می‌شود)."
        ),
        cancel_code="adm_usr",
        return_to=f"adm:users:view:{user_id}",
        fsm_state=PointsAdjustStates.reason,
        edit=False,
    )


@router.message(PointsAdjustStates.reason)
@require_bot_owner_handler
async def adm_users_points_apply(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
) -> None:
    if not _is_admin(db_user):
        await state.clear()
        return
    if _is_cancel(message):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    data = await state.get_data()
    user_id = int(data.get("admin_points_user_id") or 0)
    mode = str(data.get("admin_points_mode") or "")
    amount = int(data.get("admin_points_amount") or 0)
    request_key = str(data.get("admin_points_key") or "")
    # Local import to avoid a cycle with the large admin module.
    from app.bot.handlers.admin import _outcome_on_card, _platform_shop_user

    user, deny = await _platform_shop_user(session, user_id) if user_id else (None, feedback.NOT_FOUND)
    if deny or user is None:
        await state.clear()
        await message.answer(deny or feedback.NOT_FOUND)
        return
    if mode not in DIRECTION_LABELS or amount < 1 or len(request_key) < 8:
        await state.clear()
        await message.answer(feedback.INVALID)
        return
    try:
        reason = parse_reason(message.text)
    except ManualAdjustError as exc:
        await message.answer(html.escape(str(exc)))
        return
    delta = amount if mode == "credit" else -amount
    try:
        change = await admin_adjust_points(
            session,
            user,
            delta,
            reason=reason,
            admin_identity=f"tg:{db_user.telegram_id}",
            shop_id=None,
            request_key=request_key,
        )
    except ValueError as exc:
        await _outcome_on_card(message, state, session, user, f"⚠️ {user_safe_error(exc)}")
        return
    from app.services.manual_adjust_notices import schedule_points_notice

    balance = int(change.tx.balance_after)
    schedule_points_notice(
        user_id=int(user.id),
        mode=mode,
        points=amount,
        balance=balance,
        note=reason,
        shop_id=None,
        replayed=change.replayed,
    )
    verb = "افزوده شد" if mode == "credit" else "کسر شد"
    await _outcome_on_card(
        message,
        state,
        session,
        user,
        f"✅ {format_number(amount)} امتیاز {verb} · مانده: {format_number(balance)}",
    )


def _is_cancel(message: Message) -> bool:
    from app.bot import keyboards as kb

    return kb.is_cancel_text(message.text)
