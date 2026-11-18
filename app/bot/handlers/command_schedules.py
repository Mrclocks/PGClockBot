"""Timed switches and scenarios of the command center (``cmd:tm…``, ``cmd:sc…``, ``cmd:se:…``, ``cmd:rv:…``).

Same access as the quick switches (``command_switches.load_switch_viewer``). Every change goes
through ``services.switch_schedule``, which records what to put back in the same commit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.command_panel import SCENARIOS_CALLBACK, TIMED_CALLBACK, clock, needs_confirmation
from app.bot.handlers.command_switches import (
    SwitchViewer,
    load_revert_views,
    load_switch_viewer,
    show_command_panel,
)
from app.bot.hub_entries import COMMAND_CENTER_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.numbers import to_fa_digits
from app.services.switch_schedule import (
    DURATIONS,
    SCENARIOS,
    Scenario,
    apply_revert,
    change_for_a_while,
    end_scenario,
    revert_time,
    run_scenario,
    scenario_by_code,
)
from app.services.users import get_all_settings, on

router = Router(name="command_schedules")

TIMED_PREFIX = "cmd:tm:"
SCENARIO_PREFIX = "cmd:sc:"
END_PREFIX = "cmd:se:"
REVERT_PREFIX = "cmd:rv:"
BACK = "⬅️ بازگشت"
DURATION_LABELS = {"1h": "۱ ساعت", "3h": "۳ ساعت", "am": "تا ۸ صبح"}
TIMED_TITLE = "<b>⏱ تغییر موقت</b>\nکلیدی را انتخاب کنید؛ بعد از مدتی که تعیین می‌کنید خودش برمی‌گردد."
SCENARIOS_TITLE = "<b>🎬 سناریوها</b>\nچند کلید را با یک لمس عوض می‌کند و حالت قبلی را نگه می‌دارد تا برگردانید."


def _back_row(callback_data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=BACK, callback_data=callback_data)]


def _keys(viewer: SwitchViewer) -> frozenset[str]:
    return frozenset(switch.key for switch in viewer.switches)


def _state_word(enabled: bool) -> str:
    return "روشن" if enabled else "خاموش"


def _allowed_actions(scenario: Scenario, viewer: SwitchViewer) -> list[tuple[str, bool]]:
    return [(code, enabled) for code, enabled in scenario.actions if viewer.switch(code) is not None]


async def _viewer(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> SwitchViewer | None:
    """The viewer when they may change switches; otherwise answers «no access» and returns None."""
    viewer = await load_switch_viewer(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None or viewer.actor is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return None
    return viewer


async def _timed_list(callback: CallbackQuery, session: AsyncSession, viewer: SwitchViewer) -> None:
    ui = await get_all_settings(session, reseller_id=viewer.shop_id)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{switch.label}: {_state_word(not on(ui.get(switch.key)))} کن",
                callback_data=f"{TIMED_PREFIX}{switch.code}",
            )
        ]
        for switch in viewer.switches
    ]
    for view in await load_revert_views(session, viewer):
        if view.scenario_title is None:
            text = f"↩️ {view.switch.label}: همین حالا برگردان"
            rows.append([InlineKeyboardButton(text=text, callback_data=f"{REVERT_PREFIX}{view.revert_id}")])
    rows.append(_back_row(COMMAND_CENTER_CALLBACK))
    await safe_edit_text(callback.message, TIMED_TITLE, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _duration_choice(callback: CallbackQuery, session: AsyncSession, viewer: SwitchViewer, code: str) -> None:
    switch = viewer.switch(code)
    if switch is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    ui = await get_all_settings(session, reseller_id=viewer.shop_id)
    enabled = not on(ui.get(switch.key))
    warning = needs_confirmation(switch, enabled, ui)
    text = f"<b>{switch.label}: {_state_word(enabled)}</b>\nتا کی؟ بعد از آن خودش {_state_word(not enabled)} می‌شود."
    if warning:
        text = f"{text}\n\n⚠️ {warning}"
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"{TIMED_PREFIX}{switch.code}:{duration}")
            for duration, label in DURATION_LABELS.items()
        ],
        _back_row(TIMED_CALLBACK),
    ]
    await callback.answer()
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _apply_timed(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, viewer: SwitchViewer, code: str, duration: str
) -> None:
    switch = viewer.switch(code)
    if switch is None or duration not in DURATIONS:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    now = datetime.now(timezone.utc)
    enabled = not on((await get_all_settings(session, reseller_id=viewer.shop_id)).get(switch.key))
    revert_at = revert_time(duration, now)
    try:
        changed = await change_for_a_while(
            session, switch=switch, shop_id=viewer.shop_id, enabled=enabled, actor=viewer.actor, revert_at=revert_at
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    toast = f"{switch.label} {_state_word(enabled)} شد؛ ساعت {clock(revert_at)} برمی‌گردد" if changed else "تغییری لازم نبود"
    await callback.answer(toast)
    await show_command_panel(callback, session, state, viewer)


# A literal prefix keeps the handler visible to the callback contract check.
@router.callback_query(F.data.startswith("cmd:tm"))
async def timed_switch(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if viewer is None:
        return
    data = callback.data or ""
    if data == TIMED_CALLBACK:
        await callback.answer()
        await _timed_list(callback, session, viewer)
        return
    parts = data[len(TIMED_PREFIX):].split(":") if data.startswith(TIMED_PREFIX) else []
    if len(parts) == 1:
        await _duration_choice(callback, session, viewer, parts[0])
    elif len(parts) == 2:
        await _apply_timed(callback, session, state, viewer, parts[0], parts[1])
    else:
        await callback.answer(feedback.INVALID, show_alert=True)


async def _scenario_list(callback: CallbackQuery, session: AsyncSession, viewer: SwitchViewer) -> None:
    active = {view.scenario_title for view in await load_revert_views(session, viewer) if view.scenario_title}
    lines = [SCENARIOS_TITLE]
    rows = []
    for scenario in SCENARIOS:
        if not _allowed_actions(scenario, viewer):
            continue
        lines.append(f"\n<b>{scenario.title}</b>\n{scenario.description}")
        if scenario.title in active:
            rows.append([InlineKeyboardButton(text=f"↩️ پایان {scenario.title}", callback_data=f"{END_PREFIX}{scenario.code}")])
        else:
            rows.append([InlineKeyboardButton(text=scenario.title, callback_data=f"{SCENARIO_PREFIX}{scenario.code}")])
    rows.append(_back_row(COMMAND_CENTER_CALLBACK))
    await safe_edit_text(callback.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _scenario_confirm(callback: CallbackQuery, viewer: SwitchViewer, scenario: Scenario) -> None:
    actions = "\n".join(f"• {viewer.switch(code).label}: {_state_word(enabled)}" for code, enabled in _allowed_actions(scenario, viewer))
    back = "تا ساعت ۸ صبح؛ بعد خودش برمی‌گردد." if scenario.until_morning else "تا وقتی «پایان» را بزنید."
    text = f"<b>{scenario.title}</b>\n{scenario.description}\n\n{actions}\n\n{back}"
    rows = [
        [InlineKeyboardButton(text="✅ اجرا", callback_data=f"{SCENARIO_PREFIX}{scenario.code}:y")],
        _back_row(SCENARIOS_CALLBACK),
    ]
    await callback.answer()
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("cmd:sc"))
async def scenario_screen(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if viewer is None:
        return
    data = callback.data or ""
    if data == SCENARIOS_CALLBACK:
        await callback.answer()
        await _scenario_list(callback, session, viewer)
        return
    parts = data[len(SCENARIO_PREFIX):].split(":") if data.startswith(SCENARIO_PREFIX) else []
    scenario = scenario_by_code(parts[0]) if parts else None
    if scenario is None or not _allowed_actions(scenario, viewer) or len(parts) > 2 or parts[1:] not in ([], ["y"]):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    if len(parts) == 1:
        await _scenario_confirm(callback, viewer, scenario)
        return
    try:
        changed = await run_scenario(
            session, scenario=scenario, shop_id=viewer.shop_id, allowed=viewer.switches, actor=viewer.actor,
            now=datetime.now(timezone.utc),
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(f"{scenario.title}: {to_fa_digits(len(changed))} کلید عوض شد")
    await show_command_panel(callback, session, state, viewer)


@router.callback_query(F.data.startswith("cmd:se:"))
async def end_scenario_now(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if viewer is None:
        return
    scenario = scenario_by_code((callback.data or "")[len(END_PREFIX):])
    if scenario is None:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    restored = await end_scenario(
        session, shop_id=viewer.shop_id, scenario=scenario.code, actor=viewer.actor, allowed_keys=_keys(viewer)
    )
    await callback.answer(f"{scenario.title} تمام شد؛ {to_fa_digits(restored)} کلید برگشت")
    await show_command_panel(callback, session, state, viewer)


@router.callback_query(F.data.startswith("cmd:rv:"))
async def revert_now(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await _viewer(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if viewer is None:
        return
    raw_id = (callback.data or "")[len(REVERT_PREFIX):]
    if not raw_id.isdigit():
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    applied = await apply_revert(
        session, int(raw_id), shop_id=viewer.shop_id, actor=viewer.actor, allowed_keys=_keys(viewer)
    )
    await callback.answer("↩️ برگردانده شد" if applied else "این کلید دیگر تغییری برای برگرداندن ندارد", show_alert=not applied)
    await show_command_panel(callback, session, state, viewer)
