"""Owner-only gift-code and campaign panels in the admin marketing hub.

Light operations only: list, enable/disable a code, start/pause a campaign. Creating
and editing stays in the web panel. Every action is an explicit target state on a
shop-scoped service call, so a repeated tap changes nothing the second time.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import menu_nav as nav
from app.bot.auth import require_bot_owner_handler
from app.bot.marketing_panels import (
    CAMPAIGN_ACTION_PATTERN,
    CAMPAIGN_PANEL_CALLBACK,
    GIFT_PANEL_CALLBACK,
    GIFT_SET_PATTERN,
    campaigns_panel,
    gift_codes_panel,
    panel_page_url,
)
from app.bot.nav_inline import present_nav_panel
from app.db.models import BotUser
from app.services.campaigns import (
    CAMPAIGN_LIST_LIMIT,
    campaign_progress_map,
    change_campaign_state,
    list_recent_campaigns,
)
from app.services.gift_codes import describe_code, list_gift_codes, set_gift_code_active
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="marketing_tools")

# The hub is Owner-only and runs on the platform bot, so every call targets the platform shop.
PLATFORM_SHOP = None
GIFT_PANEL_LIMIT = 8
CAMPAIGN_NOT_FOUND = "کمپین یافت نشد یا وضعیت آن تغییر کرده است"
GIFT_NOT_FOUND = "کد یافت نشد"
DONE = "انجام شد"


async def show_gift_panel(
    message: Message, session: AsyncSession, state: FSMContext | None, *, push: bool
) -> None:
    """Edit ``message`` into the gift-code panel and record the nav level (no access check)."""
    ui = await get_all_settings(session)
    codes = await list_gift_codes(session, shop_id=PLATFORM_SHOP, limit=GIFT_PANEL_LIMIT)
    details = {code.id: describe_code(code) for code in codes}
    text, markup = gift_codes_panel(codes, details, ui=ui, web_url=panel_page_url("/gift-codes"))
    await nav.set_nav_level(state, nav.NAV_ADMIN_GIFTS, push=push)
    await present_nav_panel(message, text=text, inline=markup, state=state)


async def show_campaign_panel(
    message: Message, session: AsyncSession, state: FSMContext | None, *, push: bool
) -> None:
    """Edit ``message`` into the campaign panel and record the nav level (no access check)."""
    ui = await get_all_settings(session)
    campaigns = await list_recent_campaigns(session, shop_id=PLATFORM_SHOP, limit=CAMPAIGN_LIST_LIMIT)
    progress = await campaign_progress_map(session, [campaign.id for campaign in campaigns])
    text, markup = campaigns_panel(campaigns, progress, ui=ui, web_url=panel_page_url("/campaigns"))
    await nav.set_nav_level(state, nav.NAV_ADMIN_CAMPAIGNS, push=push)
    await present_nav_panel(message, text=text, inline=markup, state=state)


async def open_admin_marketing_panel(
    level: str,
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
    is_reseller_bot: bool = False,
) -> None:
    """Reply-navigation entry (restoring a level): same Owner gate as every admin hub."""
    # Local import: reply_nav dispatches to this module, so a top-level import is circular.
    from app.bot.handlers.reply_nav import _deny_unless_owner

    if not await _deny_unless_owner(message, session, db_user, is_reseller_bot=is_reseller_bot):
        return
    if level == nav.NAV_ADMIN_GIFTS:
        await show_gift_panel(message, session, state, push=push)
        return
    if level == nav.NAV_ADMIN_CAMPAIGNS:
        await show_campaign_panel(message, session, state, push=push)
        return
    if level == nav.NAV_ADMIN_CASHBACK:
        from app.bot.handlers.cashback_tools import open_admin_cashback_panel

        await open_admin_cashback_panel(
            message, session, db_user, state, push=push, is_reseller_bot=is_reseller_bot
        )


@router.callback_query(F.data == GIFT_PANEL_CALLBACK)
@require_bot_owner_handler
async def open_gift_panel(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is not None:
        await show_gift_panel(callback.message, session, state, push=True)


@router.callback_query(F.data.regexp(GIFT_SET_PATTERN.pattern))
@require_bot_owner_handler
async def set_gift_state(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    match = GIFT_SET_PATTERN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    changed = await set_gift_code_active(
        session, int(match.group(1)), shop_id=PLATFORM_SHOP, active=match.group(2) == "1"
    )
    await callback.answer(DONE if changed else GIFT_NOT_FOUND, show_alert=not changed)
    if callback.message is not None:
        await show_gift_panel(callback.message, session, state, push=False)


@router.callback_query(F.data == CAMPAIGN_PANEL_CALLBACK)
@require_bot_owner_handler
async def open_campaign_panel(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    await callback.answer()
    if callback.message is not None:
        await show_campaign_panel(callback.message, session, state, push=True)


@router.callback_query(F.data.regexp(CAMPAIGN_ACTION_PATTERN.pattern))
@require_bot_owner_handler
async def change_campaign(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
) -> None:
    match = CAMPAIGN_ACTION_PATTERN.match(callback.data or "")
    if match is None:
        await callback.answer()
        return
    try:
        await change_campaign_state(
            session, int(match.group(2)), shop_id=PLATFORM_SHOP, action=match.group(1)
        )
    except ValueError:
        await session.rollback()
        await callback.answer(CAMPAIGN_NOT_FOUND, show_alert=True)
    else:
        await callback.answer(DONE)
    if callback.message is not None:
        await show_campaign_panel(callback.message, session, state, push=False)
