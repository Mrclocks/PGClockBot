"""Platform owner: increase or decrease a customer's (or reseller's) wallet from the bot.

Amount first, then a required reason (min 3 chars). A reseller also gets a confirm step before
the shared reseller wallet path runs. Customer notice is scheduled after a successful commit.
"""

from __future__ import annotations

import html
import logging
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.auth import require_bot_owner_handler, require_platform_rep_mgmt
from app.bot.handlers.admin import (
    AdminStates,
    _answer_users_nav,
    _deny_if_outside_platform_shop,
    _outcome_back_to_users,
    _outcome_on_card,
    _platform_shop_user,
    _render_reseller_card,
    _render_user_card,
)
from app.config import get_settings
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_toman
from app.services.manual_adjust_notices import schedule_wallet_notice
from app.services.redact import user_safe_error
from app.services.reseller_wallet import load_reseller_profile
from app.services.shop_manual_adjust import ManualAdjustError, parse_reason

logger = logging.getLogger(__name__)
router = Router(name="admin_wallet_adjust")

REASON_PROMPT = "علت را بفرستید (حداقل ۳ کاراکتر؛ برای مشتری ارسال می‌شود)."


async def _is_payg_reseller(session: AsyncSession, user: BotUser) -> bool:
    from app.services.billing import is_payg

    return is_payg(await load_reseller_profile(session, int(user.id)))


def _reseller_wallet_confirm_text(user: BotUser, amount: int, direction: str, *, payg: bool, reason: str) -> str:
    currency = get_settings().currency
    action = "افزایش" if direction == "credit" else "کاهش"
    after = int(user.wallet_balance or 0) + (amount if direction == "credit" else -amount)
    lines = [
        f"💰 <b>{action} کیف پول نماینده</b> {html.escape(user.full_name or str(user.telegram_id))}",
        f"مبلغ: {format_toman(amount, currency)}",
        f"موجودی فعلی: {format_toman(user.wallet_balance, currency)}",
        f"علت: {html.escape(reason)}",
    ]
    if after < 0:
        lines.append("⚠️ موجودی کافی نیست؛ این کاهش انجام نمی‌شود.")
    else:
        lines.append(f"موجودی بعد از تأیید: {format_toman(after, currency)}")
        if payg and direction == "debit" and after == 0:
            lines.append("⚠️ موجودی صفر می‌شود و ادمین پاسارگارد نماینده و همهٔ کاربرانش همان لحظه قطع می‌شوند.")
    lines.append("\nاعمال شود؟")
    return "\n".join(lines)


async def _adm_users_wallet_adjust_ask(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    *,
    mode: str,
    from_reseller_card: bool = False,
) -> None:
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user = await session.get(BotUser, user_id)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import parse_admin_wallet_mode

    try:
        direction = parse_admin_wallet_mode(mode)
    except ValueError as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    await state.update_data(
        admin_credit_user_id=user_id,
        admin_wallet_mode=direction,
        admin_wallet_from_reseller=from_reseller_card,
        # One key per prompt: pressing «تأیید» twice changes the wallet once.
        admin_wallet_nonce=secrets.token_hex(8),
    )
    await callback.answer()
    from app.bot.nav_input import ask_text

    action = "افزایش" if direction == "credit" else "کاهش"
    prompt = (
        f"💰 {action} کیف پول برای <b>{html.escape(user.full_name or str(user.telegram_id))}</b>\n"
        f"موجودی فعلی: {format_toman(user.wallet_balance, get_settings().currency)}\n\n"
        "مبلغ را به تومان بفرستید (عدد مثبت، حداقل ۱۰۰۰).\n"
        "موجودی منفی مجاز نیست."
    )
    await ask_text(
        callback,
        state,
        prompt=prompt,
        cancel_code="adm_usr",
        return_to=f"adm:resellers:view:{user_id}" if from_reseller_card else f"adm:users:view:{user_id}",
        fsm_state=AdminStates.user_wallet_credit,
        edit=True,
    )


@router.callback_query(F.data.startswith("adm:users:wcredit:"))
@require_bot_owner_handler
async def adm_users_wallet_credit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    await _adm_users_wallet_adjust_ask(callback, state, session, db_user, mode="credit")


@router.callback_query(F.data.startswith("adm:users:wdebit:"))
@require_bot_owner_handler
async def adm_users_wallet_debit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    await _adm_users_wallet_adjust_ask(callback, state, session, db_user, mode="debit")


@router.callback_query(F.data.regexp(r"^adm:resellers:w(credit|debit):\d+$"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_wallet_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    mode = "credit" if ":wcredit:" in (callback.data or "") else "debit"
    await _adm_users_wallet_adjust_ask(
        callback, state, session, db_user, mode=mode, from_reseller_card=True
    )


@router.message(AdminStates.user_wallet_credit)
@require_bot_owner_handler
async def adm_users_wallet_credit_save(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    """Amount step: validate, store, then ask for the required reason."""
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message, session, db_user, feedback.CANCELLED, state, clear_state=True
        )
        return
    data = await state.get_data()
    user_id = int(data.get("admin_credit_user_id") or 0)
    mode = str(data.get("admin_wallet_mode") or "credit")
    user, deny = await _platform_shop_user(session, user_id) if user_id else (None, feedback.NOT_FOUND)
    if deny:
        await _outcome_back_to_users(message, state, session, deny)
        return
    from app.services.bot_user_admin import parse_admin_wallet_amount, parse_admin_wallet_mode

    try:
        amount = parse_admin_wallet_amount(message.text or "")
        parse_admin_wallet_mode(mode)
    except ValueError:
        await message.answer("مبلغ را به تومان و با عدد صحیح بفرستید؛ دست‌کم ۱۰۰۰.")
        return
    await state.update_data(admin_wallet_amount=int(amount))
    await state.set_state(AdminStates.user_wallet_reason)
    await message.answer(REASON_PROMPT)


@router.message(AdminStates.user_wallet_reason)
@require_bot_owner_handler
async def adm_users_wallet_reason_save(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    """Reason step: apply for a regular customer, or confirm for a reseller."""
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message, session, db_user, feedback.CANCELLED, state, clear_state=True
        )
        return
    try:
        reason = parse_reason(message.text)
    except ManualAdjustError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    user_id = int(data.get("admin_credit_user_id") or 0)
    amount = int(data.get("admin_wallet_amount") or 0)
    mode = str(data.get("admin_wallet_mode") or "credit")
    user, deny = await _platform_shop_user(session, user_id) if user_id and amount else (None, feedback.NOT_FOUND)
    if deny or user is None:
        await _outcome_back_to_users(message, state, session, deny or feedback.NOT_FOUND)
        return
    from app.services.bot_user_admin import parse_admin_wallet_mode

    direction = parse_admin_wallet_mode(mode)
    await state.update_data(admin_wallet_reason=reason)
    if await load_reseller_profile(session, int(user.id)) is not None:
        await message.answer(
            _reseller_wallet_confirm_text(
                user, amount, direction, payg=await _is_payg_reseller(session, user), reason=reason
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="✅ اعمال", callback_data="adm:rwallet:ok"),
                    InlineKeyboardButton(text="انصراف", callback_data="adm:rwallet:no"),
                ]]
            ),
        )
        return
    from app.services.bot_user_admin import admin_adjust_user_wallet

    try:
        await admin_adjust_user_wallet(
            session, user, amount, mode=direction, actor=f"tg:{db_user.telegram_id}", note=reason
        )
    except ValueError as e:
        await _outcome_on_card(message, state, session, user, f"⚠️ {user_safe_error(e)}")
        return
    await session.refresh(user)
    schedule_wallet_notice(
        user_id=int(user.id),
        mode=direction,
        amount=amount,
        balance=int(user.wallet_balance or 0),
        note=reason,
        shop_id=None,
    )
    verb = "شارژ شد" if direction == "credit" else "کسر شد"
    await _outcome_on_card(
        message,
        state,
        session,
        user,
        f"✅ کیف پول {verb}: {format_toman(amount, get_settings().currency)}",
    )


@router.callback_query(F.data.in_({"adm:rwallet:ok", "adm:rwallet:no"}))
@require_bot_owner_handler
async def adm_reseller_wallet_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    data = await state.get_data()
    user_id = int(data.get("admin_credit_user_id") or 0)
    amount = int(data.get("admin_wallet_amount") or 0)
    reason = str(data.get("admin_wallet_reason") or "").strip()
    from_reseller = bool(data.get("admin_wallet_from_reseller"))
    if not user_id or not amount or len(reason) < 3:
        await callback.answer("این درخواست منقضی شده است.", show_alert=True)
        return
    await state.clear()
    user, deny = await _platform_shop_user(session, user_id)
    if deny or user is None:
        await callback.answer(deny or feedback.NOT_FOUND, show_alert=True)
        return
    if callback.data == "adm:rwallet:no":
        await callback.answer(feedback.CANCELLED)
    else:
        from app.services.reseller_wallet import adjust_reseller_wallet, schedule_followups

        direction = str(data.get("admin_wallet_mode") or "credit")
        try:
            change = await adjust_reseller_wallet(
                session,
                user,
                amount,
                mode=direction,
                actor=f"tg:{db_user.telegram_id}",
                note=reason,
                idempotency_key=f"bot:{direction}:{user_id}:{data.get('admin_wallet_nonce')}",
            )
        except ValueError as e:
            await callback.answer(user_safe_error(e, limit=180), show_alert=True)
            return
        schedule_followups(change)
        verb = "شارژ شد" if direction == "credit" else "کسر شد"
        await callback.answer(f"کیف پول {verb}؛ مانده {change.balance:,} تومان"[:180], show_alert=True)
    if callback.message:
        await session.refresh(user)
        if from_reseller:
            await _render_reseller_card(callback.message, session, user, edit=True)
        else:
            await _render_user_card(callback.message, session, user, edit=True)
