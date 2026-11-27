"""When a group message is sent: now, or a chosen day and time in the panel's time zone (Asia/Tehran).

The form offers today and the next days by name, and a time of day, so nobody has to type a calendar date. The
chosen moment is stored in UTC on ``broadcast_logs.send_after``; the worker leaves the broadcast queued until then.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.daily_report import format_jalali_parts
from app.services.numbers import to_fa_digits

TEHRAN = ZoneInfo("Asia/Tehran")
SCHEDULE_MAX_DAYS = 7
# A time a moment away would already have passed when the worker looks; ask for a little lead.
MIN_LEAD = timedelta(minutes=2)
_TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_DAY_NAMES = {0: "امروز", 1: "فردا", 2: "پس‌فردا"}
PAST_TIME = "زمان ارسال باید دست‌کم چند دقیقه بعد از حالا باشد"
TOO_FAR = f"زمان ارسال حداکثر {to_fa_digits(SCHEDULE_MAX_DAYS)} روز بعد است"


@dataclass(frozen=True, slots=True)
class DayChoice:
    offset: int
    label: str


def _date_label(day: datetime) -> str:
    parts = format_jalali_parts(day)
    return f"{to_fa_digits(parts['day'])} {parts['month_name']}"


def day_choices(now: datetime | None = None) -> list[DayChoice]:
    """Today and the next ``SCHEDULE_MAX_DAYS`` days, named and dated in Tehran."""
    now = now or datetime.now(timezone.utc)
    choices = []
    for offset in range(SCHEDULE_MAX_DAYS + 1):
        name = _DAY_NAMES.get(offset) or f"{to_fa_digits(offset)} روز دیگر"
        choices.append(DayChoice(offset, f"{name} · {_date_label(now + timedelta(days=offset))}"))
    return choices


def checked_send_after(send_after: datetime | None, *, now: datetime | None = None) -> datetime | None:
    """``send_after`` in UTC when it is a moment in the allowed window; ``ValueError`` otherwise."""
    if send_after is None:
        return None
    if send_after.tzinfo is None:
        raise ValueError("زمان ارسال نامعتبر است")
    now = now or datetime.now(timezone.utc)
    if send_after < now + MIN_LEAD:
        raise ValueError(PAST_TIME)
    if send_after > now + timedelta(days=SCHEDULE_MAX_DAYS + 1):
        raise ValueError(TOO_FAR)
    return send_after.astimezone(timezone.utc)


def _offset(raw: object) -> int:
    text = str(raw if raw is not None else "").strip()
    if not text.isdigit() or int(text) > SCHEDULE_MAX_DAYS:
        raise ValueError("روز ارسال نامعتبر است")
    return int(text)


def send_time_from_form(form: Mapping, *, now: datetime | None = None) -> datetime | None:
    """The scheduled send time chosen on the compose form, or None to send now."""
    if str(form.get("schedule") or "") != "1":
        return None
    offset = _offset(form.get("send_day"))
    match = _TIME_RE.fullmatch(str(form.get("send_time") or "").strip())
    if match is None:
        raise ValueError("ساعت ارسال نامعتبر است")
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(TEHRAN).date() + timedelta(days=offset)
    local = datetime.combine(day, time(int(match.group(1)), int(match.group(2))), tzinfo=TEHRAN)
    return checked_send_after(local.astimezone(timezone.utc), now=now)


def schedule_label(send_after: datetime) -> str:
    """«۵ آذر، ساعت ۰۹:۳۰» in Tehran time."""
    local = send_after.astimezone(TEHRAN)
    return f"{_date_label(local)}، ساعت {to_fa_digits(local.strftime('%H:%M'))}"
