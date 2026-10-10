"""Register nv:cancel codes for staff/admin/reseller typed-input flows."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser


async def reopen_admin_plans(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin_plans import _answer_plans_cancel

    await _answer_plans_cancel(message, state, session)


async def reopen_admin_users(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin import _answer_users_nav

    await _answer_users_nav(
        message, session, db_user, "لغو شد.", state, clear_state=True
    )


async def reopen_admin_tickets(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    from app.bot.handlers.admin import _answer_tickets_nav

    await _answer_tickets_nav(
        message, session, db_user, "لغو شد.", state, clear_state=True
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
        text="لغو شد.",
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
        text="لغو شد.",
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
    from app.bot import keyboards as kb
    from app.bot.handlers.admin_settings import _keep_settings_nav
    from app.bot.handlers.reply_nav import open_admin_settings_hub
    from app.bot.nav_chrome import answer_staff_nav

    await _keep_settings_nav(state)

    async def _panel(msg, sess, user, st, **_pkw):
        await open_admin_settings_hub(msg, sess, user, st, push=False)

    await answer_staff_nav(
        message,
        session,
        db_user,
        text="لغو شد.",
        classic=kb.admin_settings_reply_keyboard(),
        state=state,
        reopen_panel=_panel,
        clear_state=True,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


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
        text="لغو شد.",
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
        text="لغو شد.",
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
        text="لغو شد.",
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
    from app.bot.handlers.plan_catalog_manage import _hub_reply_kb

    await state.set_state(None)
    await message.answer(
        "لغو شد.",
        reply_markup=await _hub_reply_kb(
            session, state, is_reseller_bot=is_reseller_bot
        ),
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
        text="لغو شد.",
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
    from app.bot import keyboards as kb
    from app.bot.handlers.reseller_settings import _lasting_kb
    from app.bot.handlers.reply_nav import open_reseller_settings_hub
    from app.bot.nav_chrome import answer_staff_nav

    async def _panel(msg, sess, user, st, **_pkw):
        await open_reseller_settings_hub(
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
        text="لغو شد.",
        classic=await _lasting_kb(
            session,
            db_user,
            kb.reseller_settings_reply_keyboard(),
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ),
        state=state,
        reopen_panel=_panel,
        clear_state=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def reopen_reseller_user_msg(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    **_kw,
) -> None:
    await state.clear()
    await message.answer("لغو شد.")


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
    from app.services.loyalty import loyalty_enabled

    data = await state.get_data()
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=bool(data.get("_loy_edit_reseller_bot")) or is_reseller_bot,
        reseller_owner_id=int(data.get("_loy_edit_owner") or 0) or reseller_owner_id,
    )
    await state.clear()
    if scope_info is None:
        await message.answer("لغو شد.")
        return
    scope, can_tiers = scope_info
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message("⭐ باشگاه مشتریان", "لغو شد — به هاب مدیریت برگشتید."),
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

    data = await state.get_data()
    user_id = int(data.get("capadj_uid") or 0)
    await state.set_state(None)
    await heal_main_reply(
        message,
        session,
        db_user,
        text="لغو شد.",
        is_reseller_bot=False,
        reseller_owner_id=None,
    )
    if user_id:
        adj_key = f"capadj:{user_id}"
        stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
        await message.answer(
            f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
            f"روز: <b>{int(stored.get('days') or 0)}</b> · "
            f"گیگ: <b>{int(stored.get('gb') or 0)}</b>",
            reply_markup=kb.admin_reseller_capacity_adjust_keyboard(
                user_id,
                days=int(stored.get("days") or 0),
                gb=int(stored.get("gb") or 0),
            ),
        )


def register_staff_cancel_codes() -> None:
    from app.bot.nav_input import CancelEntry, register_cancel_code

    register_cancel_code("adm_pln", CancelEntry(reopen=reopen_admin_plans))
    register_cancel_code("adm_usr", CancelEntry(reopen=reopen_admin_users))
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
