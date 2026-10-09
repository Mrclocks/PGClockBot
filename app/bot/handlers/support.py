from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, Ticket, TicketStatus
from app.services.formatting import format_message, ticket_status_fa
from app.services.support_contacts import (
    active_support_contacts,
    parse_support_contacts,
    support_chat_url,
)
from app.services.tickets import create_ticket, get_ticket, list_user_tickets, reply_ticket
from app.services.users import get_all_settings

router = Router(name="support")


class SupportStates(StatesGroup):
    subject = State()
    body = State()
    reply = State()


def _active_contacts_from_ui(ui: dict) -> list[dict]:
    return active_support_contacts(parse_support_contacts(ui.get("support_contacts")))


@router.callback_query(F.data == "support:home")
async def support_home(callback: CallbackQuery, session: AsyncSession):
    from app.services.rich_text import outbound_setting_text

    await callback.answer()
    ui = await get_all_settings(session)
    contacts = _active_contacts_from_ui(ui)
    from app.bot.nav_mode import is_inline_nav

    default_support = (
        "تیکت جدید بسازید یا تیکت‌های قبلی را ببینید."
        if is_inline_nav(ui)
        else "از کیبورد پایین تیکت جدید بسازید یا تیکت‌های قبلی را ببینید."
    )
    text, send_kw = outbound_setting_text(
        ui.get("support_text") or default_support,
        title="🎧 پشتیبانی",
    )
    if callback.message:
        if is_inline_nav(ui):
            from app.bot.nav_inline import support_hub_keyboard

            contact_rows: list[list[InlineKeyboardButton]] = []
            for c in contacts:
                url = support_chat_url(c.get("telegram") or "")
                title = c.get("title") or "پشتیبان"
                if url:
                    contact_rows.append(
                        [InlineKeyboardButton(text=f"💬 گفتگو با {title}", url=url)]
                    )
            await safe_edit_text(
                callback.message,
                text,
                reply_markup=support_hub_keyboard(
                    ui, contact_rows=contact_rows or None
                ),
                **send_kw,
            )
            return
        from app.bot.tg_utils import attach_reply_keyboard

        await safe_edit_text(callback.message, text, reply_markup=None, **send_kw)
        await callback.message.answer("پشتیبانی:", reply_markup=kb.support_reply_keyboard(ui))
        if contacts:
            rows: list[list[InlineKeyboardButton]] = []
            for c in contacts:
                url = support_chat_url(c.get("telegram") or "")
                title = c.get("title") or "پشتیبان"
                if url:
                    rows.append([InlineKeyboardButton(text=f"💬 گفتگو با {title}", url=url)])
            if rows:
                await callback.message.answer(
                    "ارتباط مستقیم:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                )
                await attach_reply_keyboard(
                    callback.message, kb.support_reply_keyboard(ui), text="⌨️ پشتیبانی"
                )


@router.callback_query(F.data == "support:tickets")
async def support_tickets_home(callback: CallbackQuery, session: AsyncSession):
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import support_hub_keyboard
    from app.services.rich_text import outbound_setting_text

    await callback.answer()
    ui = await get_all_settings(session)
    text, send_kw = outbound_setting_text(
        ui.get("support_text") or "پیام خود را بنویسید؛ تیم پشتیبانی پاسخ می‌دهد.",
        title="🎧 پشتیبانی — تیکت",
    )
    if callback.message:
        if is_inline_nav(ui):
            await safe_edit_text(
                callback.message,
                text,
                reply_markup=support_hub_keyboard(ui),
                **send_kw,
            )
            return
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=None,
            **send_kw,
        )
        await callback.message.answer(
            "تیکت:",
            reply_markup=kb.support_reply_keyboard(ui),
        )


@router.callback_query(F.data == "support:new")
async def support_new(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SupportStates.subject)
    if callback.message:
        await callback.message.answer("موضوع تیکت را بنویسید:", reply_markup=kb.cancel_reply())


@router.message(SupportStates.subject)
async def support_subject(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        from app.bot.tg_utils import clear_fsm_with_reply

        await clear_fsm_with_reply(
            message,
            state,
            session=session,
            db_user=db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    subject = (message.text or "").strip()
    if not subject:
        await message.answer("موضوع را به‌صورت متن بفرستید.")
        return
    await state.update_data(subject=subject)
    await state.set_state(SupportStates.body)
    await message.answer("متن پیام را بنویسید:", reply_markup=kb.cancel_reply())


@router.message(SupportStates.body)
async def support_body(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        from app.bot.tg_utils import clear_fsm_with_reply

        await clear_fsm_with_reply(
            message,
            state,
            session=session,
            db_user=db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    body = (message.text or "").strip()
    if not body:
        await message.answer("متن پیام را به‌صورت متن بفرستید.")
        return
    data = await state.get_data()
    await state.clear()
    ticket = await create_ticket(
        session,
        db_user.id,
        data.get("subject") or "پشتیبانی",
        body,
        db_user.telegram_id,
        reseller_id=reseller_owner_id,
    )
    from app.bot.menu_nav import restore_main_reply

    delivered = 0
    try:
        from app.services.notifications import notify_new_ticket

        delivered = int(
            await notify_new_ticket(
                message.bot,
                session,
                ticket_id=ticket.id,
                subject=ticket.subject,
                user_name=db_user.full_name or db_user.username,
                ticket_user_id=db_user.id,
                ticket_reseller_id=ticket.reseller_id,
            )
            or 0
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "notify_new_ticket failed ticket_id=%s reseller_id=%s",
            ticket.id,
            ticket.reseller_id,
        )
    ok_body = f"تیکت <b>#{ticket.id}</b> با موفقیت ثبت شد.\nبه‌زودی پاسخ می‌دهیم."
    if is_reseller_bot and ticket.reseller_id and delivered <= 0:
        # Ticket is saved; staff DM may have failed (never /start on shop bot, etc.)
        import logging

        logging.getLogger(__name__).warning(
            "ticket saved but staff notify delivered=0 ticket_id=%s reseller_id=%s",
            ticket.id,
            ticket.reseller_id,
        )
    await restore_main_reply(
        message,
        session,
        db_user,
        text=format_message("✅ تیکت ثبت شد", ok_body),
        state=state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data == "support:list")
async def support_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import support_hub_keyboard

    await callback.answer()
    tickets = await list_user_tickets(session, db_user.id)
    ui = await get_all_settings(session)
    if not tickets:
        text = "تیکتی ندارید."
        if callback.message:
            if is_inline_nav(ui):
                await safe_edit_text(
                    callback.message,
                    text,
                    reply_markup=support_hub_keyboard(ui),
                )
            else:
                await safe_edit_text(callback.message, text, reply_markup=None)
                await callback.message.answer(
                    text, reply_markup=kb.support_reply_keyboard(ui)
                )
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{t.id} — {ticket_status_fa(t.status)} — {t.subject[:20]}",
                callback_data=f"support:view:{t.id}",
            )
        ]
        for t in tickets[:20]
    ]
    if is_inline_nav(ui):
        rows.append(
            [
                InlineKeyboardButton(
                    text=ui.get("btn_back") or "⬅️ بازگشت",
                    callback_data="nv:s:home",
                )
            ]
        )
    text = "📋 تیکت‌های شما:"
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)
        if not is_inline_nav(ui):
            await callback.message.answer(
                "تیکت‌ها:",
                reply_markup=kb.support_reply_keyboard(ui),
            )


@router.callback_query(F.data.startswith("support:view:"))
async def support_view(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ticket_id = int(callback.data.split(":")[-1])
    ticket = await get_ticket(session, ticket_id)
    if not ticket or ticket.user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    # Shop isolation: only show tickets belonging to this bot's scope
    shop_rid = int(reseller_owner_id) if reseller_owner_id else None
    if shop_rid:
        if int(ticket.reseller_id or 0) != shop_rid:
            await callback.answer("یافت نشد", show_alert=True)
            return
    elif ticket.reseller_id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    lines = [
        f"🎫 تیکت #{ticket.id} — {ticket_status_fa(ticket.status)}",
        f"<b>{html.escape(ticket.subject or '')}</b>",
        "",
    ]
    for m in ticket.messages[-10:]:
        who = "پشتیبانی" if m.is_staff else "شما"
        lines.append(f"<b>{who}:</b> {html.escape(m.body or '')}")
    await state.set_state(SupportStates.reply)
    await state.update_data(ticket_id=ticket.id)
    if callback.message:
        await safe_edit_text(callback.message, "\n".join(lines))
        await callback.message.answer("برای پاسخ، پیام بفرستید یا انصراف بزنید:", reply_markup=kb.cancel_reply())


@router.message(SupportStates.reply)
async def support_reply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply

    if kb.is_cancel_text(message.text):
        await restore_main_reply(
            message,
            session,
            db_user,
            text="بسته شد.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    data = await state.get_data()
    ticket = await session.get(Ticket, data.get("ticket_id"))
    if not ticket or ticket.user_id != db_user.id:
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
            text="تیکت قبلاً بسته شده است.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await reply_ticket(session, ticket, message.text or "", db_user.telegram_id, is_staff=False)
    await state.clear()
    try:
        from app.services.notifications import notify_ticket_message

        await notify_ticket_message(
            message.bot,
            session,
            ticket_id=ticket.id,
            subject=ticket.subject,
            body=message.text or "",
            from_staff=False,
            ticket_user_id=ticket.user_id,
            actor_name=db_user.full_name or db_user.username,
            ticket_reseller_id=ticket.reseller_id,
        )
    except Exception:
        pass
    await restore_main_reply(
        message,
        session,
        db_user,
        text="پاسخ ثبت شد.",
        state=state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
