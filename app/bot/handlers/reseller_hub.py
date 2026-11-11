"""Shop panel home and its groups on the shop bot (``nv:res:home``, ``nv:res:grp:<id>``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import menu_nav as nav
from app.bot.nav_inline import present_nav_panel, reseller_group_hub_keyboard
from app.bot.reseller_hub_groups import GROUP_CALLBACK_PREFIX, reseller_group
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.admin_counters import PendingCounts, pending_counts
from app.services.formatting import format_message
from app.services.reseller_access import is_shop_profile_owner, load_reseller_actor
from app.services.representative_unification import shop_bot_can_manage_representatives
from app.services.screen_titles import screen_caption, screen_title_html
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="reseller_hub")


@dataclass(slots=True)
class ResellerHub:
    """What every shop panel screen needs, loaded once per tap."""

    profile: Any
    ui: dict[str, str]
    can_add_representative: bool
    show_owner_creds: bool
    queue: PendingCounts


async def load_reseller_hub(
    session: AsyncSession, db_user: BotUser, *, reseller_owner_id: int | None
) -> ResellerHub | None:
    """The shop panel of the person on this shop bot, or None without shop access."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        return None
    ui = await get_all_settings(session)
    try:
        can_add = await shop_bot_can_manage_representatives(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
            reseller_profile_id=int(getattr(profile, "id", 0) or 0) or None,
        )
    except Exception:  # an unknown answer hides the entry; the panel still opens
        logger.warning("representative check failed shop_id=%s", owner_id, exc_info=True)
        can_add = False
    # Shop-scoped only: the platform queue must never appear on a shop panel.
    try:
        queue = await pending_counts(session, shop_id=int(owner_id))
    except Exception:
        logger.warning("reseller hub pending_counts failed shop_id=%s", owner_id, exc_info=True)
        queue = PendingCounts()
    return ResellerHub(
        profile=profile,
        ui=ui,
        can_add_representative=can_add,
        show_owner_creds=is_shop_profile_owner(db_user, owner_id),
        queue=queue,
    )


async def open_reseller_group(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None,
    group_id: str,
    *,
    reseller_owner_id: int | None,
) -> None:
    group = reseller_group(group_id)
    hub = await load_reseller_hub(session, db_user, reseller_owner_id=reseller_owner_id)
    if hub is None:
        await message.answer(feedback.RESELLER_ACCESS_NOT_FOUND)
        return
    if group is None:
        await message.answer(feedback.INVALID)
        return
    if state is not None:
        await nav.set_nav_level(state, nav.NAV_RESELLER, push=False)
    await present_nav_panel(
        message,
        text=format_message(screen_title_html(hub.ui, group.screen_id), screen_caption(hub.ui, group.screen_id)),
        inline=reseller_group_hub_keyboard(
            group.id,
            hub.profile,
            hub.ui,
            can_add_representative=hub.can_add_representative,
            show_owner_creds=hub.show_owner_creds,
            pending_payments=hub.queue.payments,
            pending_tickets=hub.queue.tickets,
            pending_deliveries=hub.queue.deliveries,
        ),
        state=state,
    )


# A literal prefix keeps the handler visible to the callback contract check.
@router.callback_query(F.data.startswith("nv:res:grp:"))
async def nv_res_group(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    await callback.answer()
    if not callback.message:
        return
    if not is_reseller_bot:
        # The platform bot has no shop panel groups; its shop owner hub is one screen.
        from app.bot.handlers.reply_nav import open_reseller_home  # reply_nav imports this module

        await open_reseller_home(
            callback.message, session, db_user, state, is_reseller_bot=False, reseller_owner_id=None, push=False
        )
        return
    await open_reseller_group(
        callback.message,
        session,
        db_user,
        state,
        (callback.data or "")[len(GROUP_CALLBACK_PREFIX):],
        reseller_owner_id=reseller_owner_id,
    )
