"""Buttons of «تأیید ورود کاربران جدید»: the admins' decision, the customer's phone and retry."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.entry_gate import notify_if_due, run_entry_gate, show_gate
from app.bot.handlers.start import render_home
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.entry_approval import (
    Action,
    GateStep,
    can_decide_in_bot,
    decide,
    gate_config,
    request_again,
    resolve_gate,
    shop_key_of,
    staff_label,
)
from app.services.entry_approval_notify import (
    ADMIN_CALLBACK_PREFIX,
    CUSTOMER_ENTER_CALLBACK,
    CUSTOMER_RETRY_CALLBACK,
    parse_admin_callback,
    settle_admin_cards,
    tell_customer,
)
from app.services.purchase_contact import verify_purchase_contact
from app.services.reseller_access import effective_menu_role
from app.services.user_errors import domain_error_text
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="entry_approval")

DECIDED_ALERTS: dict[Action, str] = {
    Action.APPROVE: "✅ تأیید شد؛ منوها برای کاربر باز شد.",
    Action.REJECT: "❌ رد شد و به کاربر اطلاع داده شد.",
    Action.BLOCK: "⛔ مسدود شد و به کاربر اطلاع داده شد.",
}
PHONE_RECEIVED = "✅ شماره تماس دریافت شد."
RETRY_SENT = "درخواست دوباره ثبت شد"


def _bot_shop_id(is_reseller_bot: bool, reseller_owner_id: int | None) -> int | None:
    return reseller_owner_id if is_reseller_bot else None


@router.callback_query(F.data.startswith(ADMIN_CALLBACK_PREFIX))
async def admin_decide(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    parsed = parse_admin_callback(callback.data)
    if parsed is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    action, approval_id = parsed
    bot_shop_id = _bot_shop_id(is_reseller_bot, reseller_owner_id)
    # The request must belong to the shop of the bot the admin is tapping in; decide() checks it.
    shop_key = shop_key_of(bot_shop_id)
    if not await can_decide_in_bot(session, db_user, shop_key=shop_key, bot_shop_id=bot_shop_id):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    actor = db_user.full_name or db_user.username or str(db_user.telegram_id)
    try:
        decision = await decide(
            session, approval_id, shop_key=shop_key, action=action,
            decided_by=staff_label(username=db_user.username, user_id=db_user.telegram_id, role="bot"),
        )
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer(DECIDED_ALERTS[action], show_alert=True)
    await settle_admin_cards(session, callback.bot, decision, actor=actor)
    await tell_customer(session, decision)


@router.message(F.contact)
async def receive_entry_phone(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """The phone step of the gate; any other contact goes on to the purchase and trial handlers."""
    shop_id = _bot_shop_id(is_reseller_bot, reseller_owner_id)
    shop_key = shop_key_of(shop_id)
    ui = await get_all_settings(session)
    config = await gate_config(session, shop_key, ui)
    if config is None or not config.require_phone or message.contact is None:
        raise SkipHandler
    role = await effective_menu_role(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if role != "user":
        raise SkipHandler
    state = await resolve_gate(session, db_user, shop_key=shop_key, config=config)
    if state.step is not GateStep.NEEDS_PHONE:
        raise SkipHandler
    contact = message.contact
    try:
        await verify_purchase_contact(session, db_user, contact_user_id=contact.user_id, phone_number=contact.phone_number)
    except ValueError as exc:
        await message.answer(domain_error_text(exc))
        return
    await message.answer(PHONE_RECEIVED, reply_markup=ReplyKeyboardRemove())
    await run_entry_gate(message, session, db_user, ui=ui, shop_id=shop_id, phone=contact.phone_number)


@router.callback_query(F.data == CUSTOMER_ENTER_CALLBACK)
async def customer_enter(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        # render_home runs the gate again, so a stale button cannot open the menu.
        await render_home(
            callback.message, session, db_user, seed_reply_kb=True,
            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
        )


@router.callback_query(F.data == CUSTOMER_RETRY_CALLBACK)
async def customer_retry(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    shop_key = shop_key_of(_bot_shop_id(is_reseller_bot, reseller_owner_id))
    ui = await get_all_settings(session)
    config = await gate_config(session, shop_key, ui)
    if config is None:
        await callback.answer()
        return
    try:
        gate = await request_again(session, int(db_user.id), shop_key=shop_key, config=config)
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer(RETRY_SENT)
    await notify_if_due(callback.bot, session, db_user, gate, shop_key=shop_key)
    if callback.message:
        await show_gate(callback.message, gate, ui, config)
