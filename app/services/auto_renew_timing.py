"""When automatic renewal runs before the cutoff, and when a short wallet is reminded first.

Automatic purchases used to run only once time or volume had run out, so a customer whose wallet
was short was cut off before being told. A shop can now renew a few hours ahead, but only where
nothing is lost: a renewal plan that carries both the remaining time and volume, or a duration
pack (packs always add to the current expiry). Before that, a customer whose wallet cannot pay
gets one reminder at each configured stage, with a button that tops up exactly the shortfall.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.formatting import format_toman
from app.services.renewal_carry import CARRY_TIME, CARRY_VOLUME, plan_carries

AHEAD_HOURS_KEY = "auto_renew_ahead_hours"
FIRST_REMINDER_KEY = "auto_renew_reminder_first_hours"
LAST_REMINDER_KEY = "auto_renew_reminder_last_hours"
MAX_AHEAD_HOURS = 72
MAX_REMINDER_HOURS = 168
TIMING_DEFAULTS: dict[str, str] = {
    AHEAD_HOURS_KEY: "0",
    FIRST_REMINDER_KEY: "24",
    LAST_REMINDER_KEY: "6",
}
TIMING_BOUNDS: dict[str, tuple[int, int]] = {
    AHEAD_HOURS_KEY: (0, MAX_AHEAD_HOURS),
    FIRST_REMINDER_KEY: (0, MAX_REMINDER_HOURS),
    LAST_REMINDER_KEY: (0, MAX_REMINDER_HOURS),
}
GROUP_TITLE = "تمدید خودکار سرویس"
# (key, label, kind, help) for the web settings group; the bot shows the same keys and labels.
WEB_FIELDS: list[tuple[str, str, str, str]] = [
    (
        AHEAD_HOURS_KEY,
        "تمدید پیش از انقضا (ساعت)",
        "number",
        "تمدید خودکار این‌قدر ساعت پیش از پایان زمان سرویس انجام شود تا مشتری قطع نشود. "
        "فقط برای پلنی که زمان و حجم باقی‌مانده را منتقل می‌کند و برای بستهٔ افزایش زمان؛ "
        "بقیه هنگام انقضا تمدید می‌شوند. ۰ = هنگام انقضا. حداکثر ۷۲.",
    ),
    (
        FIRST_REMINDER_KEY,
        "یادآوری اول کمبود موجودی (ساعت مانده)",
        "number",
        "اگر تمدید خودکار روشن است و کیف پول برای آن کافی نیست، این‌قدر ساعت پیش از انقضا یک پیام "
        "با دکمهٔ شارژ همان مبلغ کسری فرستاده می‌شود. ۰ = خاموش. حداکثر ۱۶۸.",
    ),
    (
        LAST_REMINDER_KEY,
        "یادآوری آخر کمبود موجودی (ساعت مانده)",
        "number",
        "یادآوری دوم، نزدیک‌تر به انقضا. هر یادآوری در هر دورهٔ سرویس فقط یک بار می‌رود. ۰ = خاموش.",
    ),
]
BOT_FIELDS: list[tuple[str, str, str]] = [(key, label, kind) for key, label, kind, _help in WEB_FIELDS]
# The top-up handler refuses less than this, so a smaller shortfall is rounded up to it.
TOPUP_STEP = 1000
REMINDER_CODE_PREFIX = "pre"


@dataclass(frozen=True, slots=True)
class RenewTiming:
    ahead_hours: int
    reminder_hours: tuple[int, ...]


def _hours(ui: Mapping[str, Any], key: str) -> int:
    low, high = TIMING_BOUNDS[key]
    try:
        value = int(str(ui.get(key, TIMING_DEFAULTS[key])).strip())
    except (TypeError, ValueError):
        value = int(TIMING_DEFAULTS[key])
    return min(max(value, low), high)


def renew_timing(ui: Mapping[str, Any]) -> RenewTiming:
    """The shop's timing, clamped to the bounds; a reminder set to 0 is off."""
    stages = {_hours(ui, FIRST_REMINDER_KEY), _hours(ui, LAST_REMINDER_KEY)} - {0}
    return RenewTiming(ahead_hours=_hours(ui, AHEAD_HOURS_KEY), reminder_hours=tuple(sorted(stages)))


def is_active(info: Mapping[str, Any]) -> bool:
    return str(info.get("status") or "").lower() == "active"


def time_action(*, renew_enabled: bool, duration_enabled: bool, choices: Mapping[str, Any]) -> str | None:
    """The purchase that runs when time is due, the same choice ``actions_for_exhaustion`` makes."""
    if duration_enabled:
        return "duration" if "duration" in choices else None
    if renew_enabled and "renew" in choices:
        return "renew"
    return None


def renews_without_loss(action: str, choice: Any) -> bool:
    if action == "duration":
        return True
    return plan_carries(choice, CARRY_TIME) and plan_carries(choice, CARRY_VOLUME)


def is_due_ahead(
    *, action: str | None, choice: Any, expire: datetime | None, now: datetime, timing: RenewTiming
) -> bool:
    """Whether the time purchase runs now, ahead of an expiry that has not passed yet."""
    if action is None or expire is None or timing.ahead_hours <= 0 or expire <= now:
        return False
    hours_left = (expire - now).total_seconds() / 3600
    return hours_left <= timing.ahead_hours and renews_without_loss(action, choice)


def reminder_stage(*, expire: datetime, now: datetime, timing: RenewTiming) -> int | None:
    """The smallest reminder stage the time left already falls in, or None before the first."""
    hours_left = (expire - now).total_seconds() / 3600
    return next((stage for stage in timing.reminder_hours if hours_left <= stage), None)


def reminder_code(stage: int, expire: datetime) -> str:
    # Keyed by the expiry, so the next period reminds again even after a manual renewal.
    return f"{REMINDER_CODE_PREFIX}{stage}:{int(expire.timestamp())}"


def topup_amount(*, price: int, balance: int) -> int:
    """The shortfall, rounded up to the smallest amount the top-up flow accepts."""
    shortfall = max(0, int(price) - max(0, int(balance)))
    return max(TOPUP_STEP, math.ceil(shortfall / TOPUP_STEP) * TOPUP_STEP)


def reminder_text(*, label: str, hours_left: float, price: int, balance: int) -> str:
    hours = max(1, math.ceil(hours_left))
    return (
        f"⏰ حدود {hours} ساعت دیگر زمان سرویس تمام می‌شود و {label} "
        f"با <b>{format_toman(price)}</b> از کیف پول انجام می‌شود.\n"
        f"موجودی فعلی کیف پول: <b>{format_toman(max(0, balance))}</b>\n"
        "برای جلوگیری از قطع شدن سرویس، کیف پول را شارژ کنید."
    )
