"""The command center panel and its quick switches (``cmd:h``, ``cmd:t:…``, ``cmd:u:…``).

The panel shows open work, warnings and the last switch change above the switches; typing a word
on it still searches (``command_center``). Switching needs the same access as the settings screen
the switch lives on: the platform owner on the platform bot, or shop staff holding
«shop_settings» on their own shop bot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import is_bot_owner_principal
from app.bot.command_catalog import ADMIN_HOME_CALLBACK, SHOP_SETTINGS_PERMISSION
from app.bot.command_panel import (
    CONFIRM_SUFFIX,
    PendingRevertView,
    TOGGLE_PREFIX,
    UNDO_PREFIX,
    PanelData,
    needs_confirmation,
    panel_rows,
    panel_text,
    toggle_callback,
)
from app.bot.feature_staff import resolve_feature_staff
from app.bot.handlers.command_center import ALL_KINDS, ask_command_query
from app.bot.hub_entries import COMMAND_CENTER_CALLBACK
from app.bot.reseller_hub_groups import RESELLER_HOME_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.admin_counters import PendingCounts, pending_counts
from app.services.db_safe import rollback_quiet
from app.services.quick_switches import (
    OWNER_SWITCHES,
    SHOP_SWITCHES,
    QuickSwitch,
    recent_switch_changes,
)
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.staff_audit import StaffActor, actor_from_staff
from app.services.switch_schedule import pending_reverts, scenario_by_code, set_switch_by_hand, undo_by_hand
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="command_switches")

OWNER_CARDS_CALLBACK = "adm:st:sub:pay:card"
SHOP_CARDS_CALLBACK = "res:st:sub:pay:card"
TURNED_ON = "✅ {label} روشن شد"
TURNED_OFF = "⬜️ {label} خاموش شد"
ALREADY = "همین حالا هم همین‌طور است"
UNDONE = "↩️ برگردانده شد"
NOT_UNDONE = "این تغییر دیگر قابل برگرداندن نیست"
CONFIRM_YES = "✅ بله، انجام بده"
CONFIRM_NO = "انصراف"


@dataclass(frozen=True, slots=True)
class SwitchViewer:
    """Who opened the panel; ``actor`` is None (and ``switches`` empty) without settings access."""

    shop_id: int | None
    home_callback: str
    switches: tuple[QuickSwitch, ...]
    actor: StaffActor | None
    cards_callback: str | None

    def switch(self, code: str) -> QuickSwitch | None:
        return next((switch for switch in self.switches if switch.code == code), None)


async def _shop_viewer(
    session: AsyncSession, db_user: BotUser, *, reseller_owner_id: int | None
) -> SwitchViewer | None:
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        return None
    shop_id = int(owner_id)
    staff = await resolve_feature_staff(
        session, db_user, SHOP_SETTINGS_PERMISSION, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
    )
    if staff is None:
        return SwitchViewer(shop_id, RESELLER_HOME_CALLBACK, (), None, None)
    switches = tuple(s for s in SHOP_SWITCHES if s.permission is None or has_bot_perm(profile, s.permission))
    actor = actor_from_staff(staff, shop_id=shop_id)
    return SwitchViewer(shop_id, RESELLER_HOME_CALLBACK, switches, actor, SHOP_CARDS_CALLBACK)


async def load_switch_viewer(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> SwitchViewer | None:
    """The platform owner on the platform bot or shop staff on their shop bot; else None."""
    if is_reseller_bot:
        return await _shop_viewer(session, db_user, reseller_owner_id=reseller_owner_id)
    if not await is_bot_owner_principal(session, db_user, is_reseller_bot=False):
        return None
    staff = await resolve_feature_staff(session, db_user, SHOP_SETTINGS_PERMISSION)
    if staff is None:
        return SwitchViewer(None, ADMIN_HOME_CALLBACK, (), None, None)
    return SwitchViewer(None, ADMIN_HOME_CALLBACK, OWNER_SWITCHES, actor_from_staff(staff, shop_id=None), OWNER_CARDS_CALLBACK)


async def _queue(session: AsyncSession, shop_id: int | None) -> PendingCounts:
    try:
        return await pending_counts(session, shop_id=shop_id)
    except SQLAlchemyError:  # counts are a hint; the panel opens without them
        logger.warning("command center pending_counts failed shop_id=%s", shop_id, exc_info=True)
        await rollback_quiet(session)
        return PendingCounts()


async def load_revert_views(session: AsyncSession, viewer: SwitchViewer) -> list[PendingRevertView]:
    by_key = {switch.key: switch for switch in viewer.switches}
    views = []
    for row in await pending_reverts(session, shop_id=viewer.shop_id):
        switch = by_key.get(row.setting_key)
        if switch is None:
            continue
        scenario = scenario_by_code(row.scenario) if row.scenario else None
        revert_at = row.revert_at
        if revert_at is not None and revert_at.tzinfo is None:
            revert_at = revert_at.replace(tzinfo=timezone.utc)  # SQLite returns stored UTC without a zone
        views.append(
            PendingRevertView(int(row.id), switch, row.revert_value, revert_at, scenario.title if scenario else None)
        )
    return views


async def load_panel_data(session: AsyncSession, viewer: SwitchViewer) -> PanelData:
    return PanelData(
        ui=await get_all_settings(session, reseller_id=viewer.shop_id),
        queue=await _queue(session, viewer.shop_id),
        changes=await recent_switch_changes(session, shop_id=viewer.shop_id),
        now=datetime.now(timezone.utc),
        reverts=await load_revert_views(session, viewer),
    )


async def show_command_panel(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, viewer: SwitchViewer
) -> None:
    data = await load_panel_data(session, viewer)
    rows = panel_rows(data, viewer.switches, cards_callback=viewer.cards_callback)
    await ask_command_query(
        callback,
        state,
        kind_code=ALL_KINDS,
        back_callback=viewer.home_callback,
        prompt=panel_text(data, shop_id=viewer.shop_id, can_switch=bool(viewer.switches)),
        extra_inline=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _viewer_or_refuse(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> SwitchViewer | None:
    viewer = await load_switch_viewer(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
    return viewer


@router.callback_query(F.data == COMMAND_CENTER_CALLBACK)
async def open_command_panel(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer_or_refuse(
        callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        return
    await callback.answer()
    await show_command_panel(callback, session, state, viewer)


def _parse_toggle(data: str) -> tuple[str, bool, bool] | None:
    """``cmd:t:<code>:<0|1>[:y]`` → (code, enabled, confirmed); None when malformed."""
    parts = data[len(TOGGLE_PREFIX):].split(":")
    if len(parts) not in (2, 3) or parts[1] not in ("0", "1"):
        return None
    if len(parts) == 3 and parts[2] != CONFIRM_SUFFIX:
        return None
    return parts[0], parts[1] == "1", len(parts) == 3


async def _confirm(callback: CallbackQuery, switch: QuickSwitch, enabled: bool, reason: str) -> None:
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CONFIRM_YES, callback_data=toggle_callback(switch, enabled, confirmed=True))],
            [InlineKeyboardButton(text=CONFIRM_NO, callback_data=COMMAND_CENTER_CALLBACK)],
        ]
    )
    state_word = "روشن" if enabled else "خاموش"
    await safe_edit_text(callback.message, f"<b>{switch.label}: {state_word}؟</b>\n\n{reason}", reply_markup=markup)


# A literal prefix keeps the handler visible to the callback contract check.
@router.callback_query(F.data.startswith("cmd:t:"))
async def toggle_quick_switch(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer_or_refuse(
        callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        return
    parsed = _parse_toggle(callback.data or "")
    switch = viewer.switch(parsed[0]) if parsed else None
    if parsed is None or switch is None or viewer.actor is None:
        await callback.answer(feedback.NO_ACCESS if parsed else feedback.INVALID, show_alert=True)
        return
    _code, enabled, confirmed = parsed
    if not confirmed:
        reason = needs_confirmation(switch, enabled, await get_all_settings(session, reseller_id=viewer.shop_id))
        if reason:
            await callback.answer()
            await _confirm(callback, switch, enabled, reason)
            return
    changed = await set_switch_by_hand(
        session, switch=switch, shop_id=viewer.shop_id, enabled=enabled, actor=viewer.actor
    )
    toast = (TURNED_ON if enabled else TURNED_OFF).format(label=switch.label) if changed else ALREADY
    await callback.answer(toast)
    await show_command_panel(callback, session, state, viewer)


@router.callback_query(F.data.startswith("cmd:u:"))
async def undo_quick_switch(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer_or_refuse(
        callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        return
    raw_id = (callback.data or "")[len(UNDO_PREFIX):]
    if viewer.actor is None or not raw_id.isdigit():
        await callback.answer(feedback.NO_ACCESS if raw_id.isdigit() else feedback.INVALID, show_alert=True)
        return
    undone = await undo_by_hand(
        session,
        shop_id=viewer.shop_id,
        record_id=int(raw_id),
        actor=viewer.actor,
        allowed_keys=frozenset(switch.key for switch in viewer.switches),
    )
    await callback.answer(UNDONE if undone else NOT_UNDONE, show_alert=not undone)
    await show_command_panel(callback, session, state, viewer)
