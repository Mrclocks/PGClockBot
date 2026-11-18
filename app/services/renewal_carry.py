"""Which parts of a customer's remaining quota a renewal carries into the next period.

Each customer plan decides volume and time separately (``carry_remaining_volume``,
``carry_remaining_time``). An object that only has the old combined ``carry_remaining_quota``
(a paid order's frozen terms, a plan built before the split) uses it for both parts.
"""

from __future__ import annotations

from typing import Any

CARRY_VOLUME = "volume"
CARRY_TIME = "time"
CARRY_KINDS = (CARRY_VOLUME, CARRY_TIME)
CARRY_LABELS = {CARRY_VOLUME: "حجم", CARRY_TIME: "زمان"}
# Short codes for Telegram callback data (64-byte limit).
CARRY_CODES = {"v": CARRY_VOLUME, "t": CARRY_TIME}


def _column(kind: str) -> str:
    if kind not in CARRY_KINDS:
        raise ValueError("نوع انتقال نامعتبر است")
    return f"carry_remaining_{kind}"


def plan_carries(plan: Any, kind: str) -> bool:
    """Whether renewing onto ``plan`` keeps the leftover of ``kind`` (default yes)."""
    value = getattr(plan, _column(kind), None)
    if value is None:
        value = getattr(plan, "carry_remaining_quota", True)
    return bool(True if value is None else value)


def _sync_legacy(plan: Any) -> None:
    # One release keeps the combined column true only when both parts carry, so a rollback
    # to code that reads it never carries a part the admin switched off.
    plan.carry_remaining_quota = all(plan_carries(plan, kind) for kind in CARRY_KINDS)


def set_plan_carry(plan: Any, *, volume: bool, time: bool) -> None:
    plan.carry_remaining_volume = bool(volume)
    plan.carry_remaining_time = bool(time)
    _sync_legacy(plan)


def toggle_plan_carry(plan: Any, kind: str) -> bool:
    """Flip one part's switch on ``plan``; returns the new value."""
    value = not plan_carries(plan, kind)
    setattr(plan, _column(kind), value)
    _sync_legacy(plan)
    return value


def set_plan_carry_from_form(plan: Any, form: Any) -> None:
    """Read both switches of a plan form (a missing checkbox is off)."""
    set_plan_carry(
        plan, volume=bool(form.get(_column(CARRY_VOLUME))), time=bool(form.get(_column(CARRY_TIME))),
    )


def carry_status_text(plan: Any) -> str:
    """«حجم: منتقل می‌شود · زمان: منتقل نمی‌شود» for plan cards."""
    return " · ".join(
        f"{CARRY_LABELS[kind]}: {'منتقل می‌شود' if plan_carries(plan, kind) else 'منتقل نمی‌شود'}"
        for kind in CARRY_KINDS
    )


def carry_button_text(plan: Any, kind: str) -> str:
    return ("✅ " if plan_carries(plan, kind) else "⬜️ ") + f"انتقال {CARRY_LABELS[kind]} مانده در تمدید"
