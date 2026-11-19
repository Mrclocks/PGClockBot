"""The command center panel: open work, warnings, the last switch change, and the quick switches.

Rendering only; the data is loaded by the handler and every change goes through
``services.quick_switches``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton

from app.services.admin_counters import PendingCounts, format_queue_summary
from app.services.numbers import to_fa_digits
from app.services.quick_switches import PAYMENT_METHOD_KEYS, QuickSwitch, SwitchChange, switch_alerts
from app.services.users import on

TITLE = "⚡ مرکز فرمان"
SEARCH_HINT = "🔎 برای رفتن به هر بخش، نامش را بفرستید؛ مثلاً «کارت»، «تست» یا «بکاپ»."
TOGGLE_PREFIX = "cmd:t:"
UNDO_PREFIX = "cmd:u:"
CONFIRM_SUFFIX = "y"
SWITCHES_PER_ROW = 2
ON_MARK = "✅"
OFF_MARK = "⬜️"
UNDO_TEXT = "↩️ برگرداندن آخرین تغییر"
CARDS_TEXT = "💳 کارت‌های مقصد"
SCENARIOS_TEXT = "🎬 سناریوها"
TIMED_TEXT = "⏱ تغییر موقت"
SCENARIOS_CALLBACK = "cmd:sc"
TIMED_CALLBACK = "cmd:tm"
DISPLAY_ZONE = ZoneInfo("Asia/Tehran")


@dataclass(frozen=True, slots=True)
class PendingRevertView:
    """A switch that will be put back: shown on the panel so nobody is surprised by the change."""

    revert_id: int
    switch: QuickSwitch
    revert_value: bool
    revert_at: datetime | None
    scenario_title: str | None


@dataclass(frozen=True, slots=True)
class PanelData:
    ui: dict[str, str]
    queue: PendingCounts
    changes: list[SwitchChange]
    now: datetime
    reverts: list[PendingRevertView] = field(default_factory=list)


def toggle_callback(switch: QuickSwitch, enabled: bool, *, confirmed: bool = False) -> str:
    base = f"{TOGGLE_PREFIX}{switch.code}:{int(enabled)}"
    return f"{base}:{CONFIRM_SUFFIX}" if confirmed else base


def _ago(now: datetime, at: datetime | None) -> str:
    if at is None:
        return ""
    minutes = max(0, int((now - at).total_seconds() // 60))
    if minutes < 60:
        return f"{to_fa_digits(minutes)} دقیقه پیش"
    if minutes < 24 * 60:
        return f"{to_fa_digits(minutes // 60)} ساعت پیش"
    return f"{to_fa_digits(minutes // (24 * 60))} روز پیش"


def _change_line(change: SwitchChange, now: datetime) -> str:
    state = "روشن" if change.enabled else "خاموش"
    who = html.escape(change.actor_label)
    return f"🕘 آخرین تغییر: {change.switch.label} {state} شد · {who} · {_ago(now, change.at)}"


def panel_text(data: PanelData, *, shop_id: int | None, can_switch: bool) -> str:
    lines = [f"<b>{TITLE}</b>"]
    queue_line = format_queue_summary(data.queue)
    if queue_line:
        lines.append(queue_line)
    alerts = switch_alerts(data.ui, data.changes, shop_id=shop_id, now=data.now)
    if alerts:
        lines.extend(["", *alerts])
    if can_switch and data.changes:
        lines.extend(["", _change_line(data.changes[0], data.now)])
    if data.reverts:
        lines.extend(["", *(revert_line(view) for view in data.reverts)])
    lines.extend(["", SEARCH_HINT])
    return "\n".join(lines)


def clock(at: datetime) -> str:
    return to_fa_digits(at.astimezone(DISPLAY_ZONE).strftime("%H:%M"))


def revert_line(view: PendingRevertView) -> str:
    state = "روشن" if view.revert_value else "خاموش"
    when = f"ساعت {clock(view.revert_at)}" if view.revert_at else "با پایان سناریو"
    source = f" ({view.scenario_title})" if view.scenario_title else ""
    return f"⏱ {view.switch.label}{source} {when} دوباره {state} می‌شود."


def undoable_change(data: PanelData) -> SwitchChange | None:
    """The latest change, while its switch still holds the value that change set."""
    if not data.changes:
        return None
    latest = data.changes[0]
    return latest if on(data.ui.get(latest.switch.key)) == latest.enabled else None


def panel_rows(
    data: PanelData, switches: tuple[QuickSwitch, ...], *, cards_callback: str | None
) -> list[list[InlineKeyboardButton]]:
    """Switch buttons (each sets the opposite of what it shows), then cards and undo."""
    buttons = []
    for switch in switches:
        is_on = on(data.ui.get(switch.key))
        buttons.append(
            InlineKeyboardButton(
                text=f"{ON_MARK if is_on else OFF_MARK} {switch.label}", callback_data=toggle_callback(switch, not is_on)
            )
        )
    rows = [buttons[i : i + SWITCHES_PER_ROW] for i in range(0, len(buttons), SWITCHES_PER_ROW)]
    if switches:
        rows.append(
            [
                InlineKeyboardButton(text=SCENARIOS_TEXT, callback_data=SCENARIOS_CALLBACK),
                InlineKeyboardButton(text=TIMED_TEXT, callback_data=TIMED_CALLBACK),
            ]
        )
    if cards_callback:
        rows.append([InlineKeyboardButton(text=CARDS_TEXT, callback_data=cards_callback)])
    change = undoable_change(data)
    if change is not None and change.switch.key in {switch.key for switch in switches}:
        rows.append([InlineKeyboardButton(text=UNDO_TEXT, callback_data=f"{UNDO_PREFIX}{change.record_id}")])
    return rows


def needs_confirmation(switch: QuickSwitch, enabled: bool, ui: dict[str, str]) -> str | None:
    """Why this change deserves a second tap, or None: closing the shop or its last way to pay."""
    if switch.key == "shop_maintenance_enabled" and enabled:
        return "با روشن شدن حالت تعمیرات، خرید جدید برای همهٔ مشتری‌ها بسته می‌شود. تمدید و پشتیبانی باز می‌ماند."
    if switch.key in PAYMENT_METHOD_KEYS and not enabled:
        others_on = [key for key in PAYMENT_METHOD_KEYS if key != switch.key and on(ui.get(key))]
        if not others_on:
            return "این آخرین روش پرداخت روشن است. با خاموش شدنش هیچ مشتری‌ای نمی‌تواند پرداخت کند."
    return None
