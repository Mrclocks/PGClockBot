"""Inline reply/close actions on Telegram ticket notification messages."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin
from app.db.models import BotUser, Ticket, TicketStatus
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.tickets import close_ticket, get_ticket, reply_ticket
from app.services import bot_feedback_texts as feedback

logger = logging.getLogger(__name__)
router = Router(name="ticket_actions")


class TicketActionStates(StatesGroup):
    reply = State()


async def _actor_role(
    session: AsyncSession,
    db_user: BotUser,
    ticket: Ticket,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> str | None:
    """Return 'staff' | 'owner' | None for this ticket."""
    shop_rid = getattr(ticket, "reseller_id", None)
    if is_platform_admin(db_user) and not is_reseller_bot:
        # Platform staff may only handle platform tickets — never shop tickets
        if shop_rid:
            return None
        return "staff"
    if ticket.user_id == db_user.id:
        return "owner"
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not has_bot_perm(profile, "tickets"):
        return None
    if shop_rid is not None and int(shop_rid) == int(owner_id):
        return "staff"
    return None


def _thread_preview(ticket: Ticket, *, as_staff: bool) -> str:
    lines = [
        f"🎫 تیکت #{ticket.id} — {html.escape(ticket.subject or '')}",
        "",
    ]
    for m in (ticket.messages or [])[-8:]:
        if as_staff:
            who = "پشتیبانی" if m.is_staff else "کاربر"
        else:
            who = "پشتیبانی" if m.is_staff else "شما"
        lines.append(f"<b>{who}:</b> {html.escape(m.body or '')}")
    lines.append("")
    lines.append("پاسخ را بنویسید:")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("tkt:reply:"))
async def tkt_reply_start(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    try:
        ticket_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    ticket = await get_ticket(session, ticket_id)
    if not ticket:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    role = await _actor_role(
        session,
        db_user,
        ticket,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not role:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    if ticket.status == TicketStatus.CLOSED.value:
        await callback.answer(feedback.TICKET_CLOSED, show_alert=True)
        return
    await callback.answer()
    as_staff = role == "staff"
    # Prefer reopening the staff tickets list after cancel when acting as staff.
    reopen = "res_tickets" if (as_staff and is_reseller_bot) else (
        "adm_tickets" if as_staff else "main"
    )
    await state.update_data(
        ticket_id=ticket.id, as_staff=as_staff, tkt_cancel_reopen=reopen
    )
    from app.bot.nav_input import ask_text

    text = _thread_preview(ticket, as_staff=as_staff)
    await ask_text(
        callback,
        state,
        prompt=text,
        cancel_code="tkt_rpl",
        fsm_state=TicketActionStates.reply,
        edit=True,
    )


async def _tkt_cancel_reopen(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    text: str,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> None:
    """Heal main KB and resend the previous tickets panel when possible."""
    from app.bot.nav_chrome import answer_staff_nav, heal_main_reply

    data = await state.get_data()
    reopen = str(data.get("tkt_cancel_reopen") or "main")
    try:
        await state.clear()
    except Exception:
        logger.debug("_tkt_cancel_reopen: ignored failure", exc_info=True)
    if reopen == "adm_tickets":
        from app.bot.handlers.admin import _present_admin_tickets_list

        async def _reopen(msg, sess, user, st, **_kw):
            await _present_admin_tickets_list(msg, sess, edit=False)

        await answer_staff_nav(
            message,
            session,
            db_user,
            text=text,
            classic=None,
            state=None,
            reopen_panel=_reopen,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return
    if reopen == "res_tickets":
        from app.bot.handlers.reseller import present_reseller_tickets_list

        async def _reopen_res(msg, sess, user, st, **_kw):
            await present_reseller_tickets_list(
                msg,
                sess,
                user,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )

        await answer_staff_nav(
            message,
            session,
            db_user,
            text=text,
            classic=None,
            state=None,
            reopen_panel=_reopen_res,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await heal_main_reply(
        message,
        session,
        db_user,
        text=text,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.message(TicketActionStates.reply)
async def tkt_reply_body(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply
    from app.bot.nav_input import finish_text_step, try_legacy_cancel

    if kb.is_cancel_text(message.text):
        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await _tkt_cancel_reopen(
            message,
            session,
            db_user,
            state,
            text=feedback.CANCELLED,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    body = (message.text or "").strip()
    if not body:
        await message.answer("پاسخ را به‌صورت متن بفرستید.")
        return
    data = await state.get_data()
    ticket = await session.get(Ticket, data.get("ticket_id"))
    if not ticket:
        await restore_main_reply(
            message,
            session,
            db_user,
            text="تیکت نامعتبر",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if ticket.status == TicketStatus.CLOSED.value:
        await restore_main_reply(
            message,
            session,
            db_user,
            text=feedback.TICKET_ALREADY_CLOSED,
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    as_staff = bool(data.get("as_staff"))
    # Re-validate staff ACL on every reply (role may have changed mid-FSM)
    if as_staff:
        role = await _actor_role(
            session,
            db_user,
            ticket,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        if role != "staff":
            await restore_main_reply(
                message,
                session,
                db_user,
                text=feedback.NO_ACCESS_REPLY,
                state=state,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
    elif ticket.user_id != db_user.id:
        await restore_main_reply(
            message,
            session,
            db_user,
            text=feedback.NO_ACCESS_REPLY,
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await reply_ticket(session, ticket, body, db_user.telegram_id, is_staff=as_staff)
    try:
        from app.services.notifications import notify_ticket_message

        await notify_ticket_message(
            message.bot,
            session,
            ticket_id=ticket.id,
            subject=ticket.subject,
            body=body,
            from_staff=as_staff,
            ticket_user_id=ticket.user_id,
            actor_name=db_user.full_name or db_user.username,
            ticket_reseller_id=ticket.reseller_id,
        )
    except Exception:
        logger.debug("tkt_reply_body: ignored failure", exc_info=True)
    if as_staff:
        await _tkt_cancel_reopen(
            message,
            session,
            db_user,
            state,
            text=feedback.SENT,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await finish_text_step(
        message,
        state,
        text=feedback.SENT,
        inline=InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, "support:list")]]),
        keep_input=True,
    )


@router.callback_query(F.data.startswith("tkt:close:"))
async def tkt_close(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    try:
        ticket_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    ticket = await get_ticket(session, ticket_id)
    if not ticket:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    role = await _actor_role(
        session,
        db_user,
        ticket,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not role:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    if ticket.status == TicketStatus.CLOSED.value:
        await callback.answer(feedback.ALREADY_CLOSED, show_alert=True)
        await _drop_ticket_buttons(callback, ticket_id)
        return
    await close_ticket(session, ticket)
    await callback.answer(feedback.TICKET_CLOSED_DONE, show_alert=True)
    await _drop_ticket_buttons(callback, ticket_id)


async def _drop_ticket_buttons(callback: CallbackQuery, ticket_id: int) -> None:
    """Remove the closed ticket's reply/close buttons; a list keeps its other rows and «بازگشت»."""
    message = callback.message
    markup = getattr(message, "reply_markup", None) if message else None
    if message is None or markup is None:
        return
    suffix = f":{int(ticket_id)}"
    rows = [
        row
        for row in markup.inline_keyboard
        if not any((b.callback_data or "").startswith("tkt:") and (b.callback_data or "").endswith(suffix) for b in row)
    ]
    try:
        await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    except TelegramBadRequest:
        logger.debug("ticket buttons already gone", exc_info=True)


def _register_ticket_cancel_codes() -> None:
    from app.bot.nav_input import CancelEntry, register_cancel_code

    async def _reopen_ticket_reply(message, session, db_user, state, **kw):
        await _tkt_cancel_reopen(
            message,
            session,
            db_user,
            state,
            text=feedback.CANCELLED,
            is_reseller_bot=kw.get("is_reseller_bot", False),
            reseller_owner_id=kw.get("reseller_owner_id"),
        )

    register_cancel_code("tkt_rpl", CancelEntry(reopen=_reopen_ticket_reply))


_register_ticket_cancel_codes()
