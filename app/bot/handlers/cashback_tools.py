"""Cashback light panel in the bot: list, feature toggle, rule enable/disable.

Create and edit stay in the web panel. Platform Owner uses the marketing hub
(with a nav level, like gift codes). A shop actor with the ``cashback``
permission uses the shop hub the same way shop broadcast does: clear FSM and
present the panel with the group Back button.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import menu_nav as nav
from app.bot.auth import can_shop_feature, require_bot_owner_handler
from app.bot.marketing_panels import (
    CASHBACK_FEATURE_PATTERN,
    CASHBACK_PANEL_CALLBACK,
    CASHBACK_RULE_PATTERN,
    SHOP_CASHBACK_FEATURE_PATTERN,
    SHOP_CASHBACK_PANEL_CALLBACK,
    SHOP_CASHBACK_RULE_PATTERN,
    cashback_panel,
    panel_page_url,
)
from app.bot.nav_inline import present_nav_panel
from app.bot.reseller_hub_groups import back_callback_for
from app.db.models import BotUser, Role
from app.services.cashback_rules import (
    CashbackSettings,
    budget_used,
    list_rules,
    load_settings,
    save_settings,
    set_rule_enabled,
)
from app.services.db_safe import rollback_quiet
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="cashback_tools")

DONE = "انجام شد"
RULE_NOT_FOUND = "قانون یافت نشد"
NO_ACCESS = "دسترسی مدیریت کش‌بک ندارید"


async def resolve_cashback_shop(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> tuple[int | None, bool]:
    """``(shop_id, allowed)`` — ``shop_id`` is ``None`` for the platform shop."""
    if is_reseller_bot:
        from app.services.reseller_access import load_reseller_actor

        owner_id, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        )
        if not owner_id or not profile:
            return None, False
        if not can_shop_feature("cashback", profile=profile, db_user=db_user):
            return None, False
        return int(owner_id), True
    if db_user.role == Role.ADMIN.value:
        return None, True
    return None, False


async def show_cashback_panel(
    message: Message,
    session: AsyncSession,
    state: FSMContext | None,
    *,
    shop_id: int | None,
    push: bool,
    back_callback: str,
    feature_prefix: str,
    rule_prefix: str,
    nav_level: str | None = None,
) -> None:
    """Edit ``message`` into the cashback panel (caller already checked access)."""
    ui = await get_all_settings(session)
    settings = await load_settings(session, shop_id)
    rules = await list_rules(session, shop_id)
    used = await budget_used(session, shop_id)
    text, markup = cashback_panel(
        settings,
        rules,
        budget_used_amount=used,
        ui=ui,
        web_url=panel_page_url("/cashback"),
        back_callback=back_callback,
        feature_prefix=feature_prefix,
        rule_prefix=rule_prefix,
    )
    if state is not None and nav_level is not None:
        await nav.set_nav_level(state, nav_level, push=push)
    await present_nav_panel(message, text=text, inline=markup, state=state)


async def open_admin_cashback_panel(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    """Reply-navigation restore for the Owner cashback panel."""
    from app.bot.handlers.reply_nav import _deny_unless_owner

    if not await _deny_unless_owner(message, session, db_user, is_reseller_bot=is_reseller_bot):
        return
    await show_cashback_panel(
        message,
        session,
        state,
        shop_id=None,
        push=push,
        back_callback="nv:adm:marketing",
        feature_prefix="adm:cb",
        rule_prefix="adm:cb",
        nav_level=nav.NAV_ADMIN_CASHBACK,
    )


async def open_shop_cashback_panel(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Shop hub leaf: broadcast-style (clear FSM; Back returns to the shop group)."""
    _ = push
    shop_id, allowed = await resolve_cashback_shop(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not allowed:
        await message.answer(NO_ACCESS)
        return
    if state is not None:
        await state.clear()
    await show_cashback_panel(
        message,
        session,
        state,
        shop_id=shop_id,
        push=False,
        back_callback=back_callback_for("res_cashback"),
        feature_prefix="res:cb",
        rule_prefix="res:cb",
        nav_level=None,
    )


async def _set_feature(
    session: AsyncSession, shop_id: int | None, *, enabled: bool
) -> CashbackSettings:
    current = await load_settings(session, shop_id)
    updated = CashbackSettings(
        enabled=enabled,
        combine=current.combine,
        hold_hours=current.hold_hours,
        monthly_budget=current.monthly_budget,
    )
    await save_settings(session, shop_id, updated)
    return updated


async def _try_set_rule(
    session: AsyncSession, shop_id: int | None, rule_id: int, *, enabled: bool
) -> bool:
    try:
        await set_rule_enabled(session, shop_id, rule_id, enabled)
    except ValueError:
        await rollback_quiet(session)
        return False
    return True


@router.callback_query(F.data == CASHBACK_PANEL_CALLBACK)
@require_bot_owner_handler
async def open_owner_cashback(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is not None:
        await show_cashback_panel(
            callback.message,
            session,
            state,
            shop_id=None,
            push=True,
            back_callback="nv:adm:marketing",
            feature_prefix="adm:cb",
            rule_prefix="adm:cb",
            nav_level=nav.NAV_ADMIN_CASHBACK,
        )


@router.callback_query(F.data.regexp(CASHBACK_FEATURE_PATTERN.pattern))
@require_bot_owner_handler
async def owner_set_feature(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    match = CASHBACK_FEATURE_PATTERN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    await _set_feature(session, None, enabled=match.group(1) == "1")
    await callback.answer(DONE)
    if callback.message is not None:
        await show_cashback_panel(
            callback.message,
            session,
            state,
            shop_id=None,
            push=False,
            back_callback="nv:adm:marketing",
            feature_prefix="adm:cb",
            rule_prefix="adm:cb",
            nav_level=nav.NAV_ADMIN_CASHBACK,
        )


@router.callback_query(F.data.regexp(CASHBACK_RULE_PATTERN.pattern))
@require_bot_owner_handler
async def owner_set_rule(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    match = CASHBACK_RULE_PATTERN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    ok = await _try_set_rule(
        session, None, int(match.group(1)), enabled=match.group(2) == "1"
    )
    await callback.answer(DONE if ok else RULE_NOT_FOUND, show_alert=not ok)
    if callback.message is not None:
        await show_cashback_panel(
            callback.message,
            session,
            state,
            shop_id=None,
            push=False,
            back_callback="nv:adm:marketing",
            feature_prefix="adm:cb",
            rule_prefix="adm:cb",
            nav_level=nav.NAV_ADMIN_CASHBACK,
        )


@router.callback_query(F.data == SHOP_CASHBACK_PANEL_CALLBACK)
async def open_shop_cashback_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await open_shop_cashback_panel(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data.regexp(SHOP_CASHBACK_FEATURE_PATTERN.pattern))
async def shop_set_feature(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    match = SHOP_CASHBACK_FEATURE_PATTERN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    shop_id, allowed = await resolve_cashback_shop(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not allowed:
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    await _set_feature(session, shop_id, enabled=match.group(1) == "1")
    await callback.answer(DONE)
    if callback.message is not None:
        await show_cashback_panel(
            callback.message,
            session,
            state,
            shop_id=shop_id,
            push=False,
            back_callback=back_callback_for("res_cashback"),
            feature_prefix="res:cb",
            rule_prefix="res:cb",
        )


@router.callback_query(F.data.regexp(SHOP_CASHBACK_RULE_PATTERN.pattern))
async def shop_set_rule(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    match = SHOP_CASHBACK_RULE_PATTERN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    shop_id, allowed = await resolve_cashback_shop(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not allowed:
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    ok = await _try_set_rule(
        session, shop_id, int(match.group(1)), enabled=match.group(2) == "1"
    )
    await callback.answer(DONE if ok else RULE_NOT_FOUND, show_alert=not ok)
    if callback.message is not None:
        await show_cashback_panel(
            callback.message,
            session,
            state,
            shop_id=shop_id,
            push=False,
            back_callback=back_callback_for("res_cashback"),
            feature_prefix="res:cb",
            rule_prefix="res:cb",
        )
