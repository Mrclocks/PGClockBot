"""Register nv:cancel codes for staff/admin/reseller typed-input flows."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.button_color_editor import PLATFORM_SCOPE
from app.services.screen_titles import screen_title_html
from app.services.users import get_all_settings


async def reopen_admin_plans(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin_plans import _answer_plans_cancel

    await _answer_plans_cancel(message, state, session, db_user)


async def reopen_admin_users(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin import _answer_users_nav

    await _answer_users_nav(
        message, session, db_user, feedback.CANCELLED, state, clear_state=True
    )


async def reopen_admin_resellers(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.nav_inline import present_nav_panel

    await state.clear()
    await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)


async def reopen_admin_tickets(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin import _answer_tickets_nav

    await _answer_tickets_nav(
        message, session, db_user, feedback.CANCELLED, state, clear_state=True
    )


async def reopen_admin_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.reply_nav import open_admin_home
    from app.bot.nav_chrome import answer_staff_nav

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        classic=None,
        state=state,
        reopen_panel=open_admin_home,
        clear_state=True,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


async def reopen_admin_broadcast(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.reply_nav import open_admin_broadcast_hub
    from app.bot.nav_chrome import answer_staff_nav

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        classic=None,
        state=state,
        reopen_panel=open_admin_broadcast_hub,
        clear_state=True,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


async def reopen_admin_settings(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin_settings import reopen_settings_location

    await reopen_settings_location(message, session, db_user, state)


async def reopen_admin_backup(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot import keyboards as kb
    from app.bot.handlers.admin_backup import _lasting_kb
    from app.bot.handlers.reply_nav import open_admin_backup_hub
    from app.bot.nav_chrome import answer_staff_nav

    async def _panel(msg, sess, user, st, **_pkw):
        await open_admin_backup_hub(msg, sess, user, st, push=False)

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        classic=await _lasting_kb(session, db_user, kb.admin_system_reply_keyboard()),
        state=state,
        reopen_panel=_panel,
        clear_state=True,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


async def reopen_pg_users(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    **_kw,
) -> None:
    from app.bot.auth import filtered_pg_reply_keyboard
    from app.bot.handlers.reply_nav import open_pg_home
    from app.bot.nav_chrome import answer_staff_nav

    classic = await filtered_pg_reply_keyboard(
        db_user, session=session, is_reseller_bot=is_reseller_bot
    )

    async def _panel(msg, sess, user, st, **_pkw):
        await open_pg_home(
            msg,
            sess,
            user,
            st,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        classic=classic,
        state=state,
        reopen_panel=_panel,
        clear_state=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def reopen_pg_nodes(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot import keyboards as kb
    from app.bot.handlers.admin_pg_nodes import _lasting_kb
    from app.bot.handlers.reply_nav import open_admin_product_hub
    from app.bot.nav_chrome import answer_staff_nav

    async def _panel(msg, sess, user, st, **_pkw):
        await open_admin_product_hub(msg, sess, user, st, push=False)

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        classic=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()),
        state=state,
        reopen_panel=_panel,
        clear_state=True,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


async def reopen_plan_catalog(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    **_kw,
) -> None:
    """Cancel from pcm ask_text — lasting main KB + reopen categories inline."""
    from app.bot.handlers.plan_catalog_manage import open_categories_manage
    from app.bot.nav_chrome import answer_staff_nav

    await state.set_state(None)

    async def _panel(msg, sess, user, st, **_pkw):
        await open_categories_manage(
            msg,
            sess,
            user,
            st,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        state=state,
        reopen_panel=_panel,
        clear_state=False,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def reopen_reseller_plans(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    **_kw,
) -> None:
    from app.bot import keyboards as kb
    from app.bot.handlers.reply_nav import open_reseller_plans_hub
    from app.bot.nav_chrome import answer_staff_nav

    async def _panel(msg, sess, user, st, **_pkw):
        await open_reseller_plans_hub(
            msg,
            sess,
            user,
            st,
            push=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=feedback.CANCELLED,
        classic=kb.reseller_plans_reply_keyboard(),
        state=state,
        reopen_panel=_panel,
        clear_state=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def reopen_reseller_settings(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    **_kw,
) -> None:
    from app.bot.handlers.reseller_settings import reopen_shop_settings_location

    await reopen_shop_settings_location(
        message, session, db_user, state, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )


async def reopen_reseller_user_msg(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.nav_inline import present_nav_panel

    await state.clear()
    await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)


async def reopen_loyalty_staff(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    **_kw,
) -> None:
    from app.bot.handlers.loyalty import _staff_loyalty_answer, resolve_loyalty_manage_scope
    from app.services.formatting import format_message

    data = await state.get_data()
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=bool(data.get("_loy_edit_reseller_bot")) or is_reseller_bot,
        reseller_owner_id=int(data.get("_loy_edit_owner") or 0) or reseller_owner_id,
    )
    await state.clear()
    if scope_info is None:
        from app.bot.nav_inline import present_nav_panel

        await present_nav_panel(message, text=feedback.CANCELLED, inline=None, state=state)
        return
    scope, can_tiers = scope_info
    # The club hub belongs to the platform (scope None) or to one shop.
    ui = await get_all_settings(session, reseller_id=scope or PLATFORM_SCOPE)
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message(screen_title_html(ui, "a.loyalty"), feedback.CANCELLED_BACK_TO_HUB),
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        heal_reply=True,
    )
    _ = scope


async def reopen_reseller_cap_adjust(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot import keyboards as kb
    from app.bot.nav_chrome import heal_main_reply
    from app.bot.nav_inline import is_bot_panel_message, present_nav_panel

    data = await state.get_data()
    user_id = int(data.get("capadj_uid") or 0)
    await state.set_state(None)
    if not is_bot_panel_message(message):
        await heal_main_reply(
            message,
            session,
            db_user,
            text=feedback.CANCELLED,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
    if not user_id:
        return
    adj_key = f"capadj:{user_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    gb = int(stored.get("gb") or 0)
    await present_nav_panel(
        message,
        text=(
            f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
            f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>"
        ),
        inline=kb.admin_reseller_capacity_adjust_keyboard(user_id, days=days, gb=gb),
        state=state,
    )


def register_staff_cancel_codes() -> None:
    from app.bot.nav_input import CancelEntry, register_cancel_code

    register_cancel_code("adm_pln", CancelEntry(reopen=reopen_admin_plans))
    register_cancel_code("adm_usr", CancelEntry(reopen=reopen_admin_users))
    register_cancel_code("adm_res", CancelEntry(reopen=reopen_admin_resellers))
    register_cancel_code("adm_tkt", CancelEntry(reopen=reopen_admin_tickets))
    register_cancel_code("adm_home", CancelEntry(reopen=reopen_admin_home))
    register_cancel_code("adm_bc", CancelEntry(reopen=reopen_admin_broadcast))
    register_cancel_code("adm_set", CancelEntry(reopen=reopen_admin_settings))
    register_cancel_code("adm_bak", CancelEntry(reopen=reopen_admin_backup))
    register_cancel_code("adm_pg_u", CancelEntry(reopen=reopen_pg_users))
    register_cancel_code("adm_pg_n", CancelEntry(reopen=reopen_pg_nodes))
    register_cancel_code("pcm_hub", CancelEntry(reopen=reopen_plan_catalog))
    register_cancel_code("rs_pln", CancelEntry(reopen=reopen_reseller_plans))
    register_cancel_code("rs_set", CancelEntry(reopen=reopen_reseller_settings))
    register_cancel_code("rs_msg", CancelEntry(reopen=reopen_reseller_user_msg))
    register_cancel_code("loy_stf", CancelEntry(reopen=reopen_loyalty_staff))
    register_cancel_code("adm_cap", CancelEntry(reopen=reopen_reseller_cap_adjust))


register_staff_cancel_codes()
