"""Validated Tehran date/time windows and bounded chart intervals for finance reports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services.dashboard_trend import TEHRAN, WEEKDAYS_FA

MAX_REPORT_DAYS = 366
MIN_REPORT_YEAR = 2000
MAX_REPORT_YEAR = 9998
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
PERIOD_LABELS = {"day": "امروز", "week": "۷ روز اخیر", "month": "۳۰ روز اخیر", "custom": "بازهٔ دلخواه"}
LOCAL_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class FinanceReportRange:
    period: str
    start: datetime
    end: datetime

    @property
    def previous_start(self) -> datetime:
        return self.start - (self.end - self.start)

    @property
    def start_input(self) -> str:
        return self.start.astimezone(TEHRAN).strftime("%Y-%m-%dT%H:%M")

    @property
    def end_input(self) -> str:
        return self.end.astimezone(TEHRAN).strftime("%Y-%m-%dT%H:%M")


def _parse_local(raw: str) -> datetime:
    if not LOCAL_DATETIME.fullmatch(raw):
        raise ValueError("تاریخ و ساعت شروع و پایان را کامل و معتبر وارد کنید.")
    try:
        local = datetime.strptime(raw, "%Y-%m-%dT%H:%M").replace(tzinfo=TEHRAN)
    except ValueError as exc:
        raise ValueError("تاریخ و ساعت واردشده معتبر نیست.") from exc
    if not MIN_REPORT_YEAR <= local.year <= MAX_REPORT_YEAR:
        raise ValueError("سال میلادی باید بین ۲۰۰۰ و ۹۹۹۸ باشد.")
    return local.astimezone(timezone.utc)


def resolve_finance_range(
    *, period: str = "week", start: str = "", end: str = "", now: datetime | None = None,
) -> FinanceReportRange:
    moment = (now or datetime.now(timezone.utc)).astimezone(TEHRAN).replace(second=0, microsecond=0)
    if period == "custom":
        start_at, end_at = _parse_local(start), _parse_local(end)
        if start_at >= end_at:
            raise ValueError("پایان بازه باید بعد از شروع آن باشد.")
        if end_at - start_at > timedelta(days=MAX_REPORT_DAYS):
            raise ValueError("بازهٔ گزارش حداکثر ۳۶۶ روز است.")
        return FinanceReportRange("custom", start_at, end_at)
    key = period if period in PERIOD_DAYS else "week"
    midnight = moment.replace(hour=0, minute=0)
    # Presets include the current minute; a custom end is always exclusive.
    end_at = moment + timedelta(minutes=1)
    start_at = midnight - timedelta(days=PERIOD_DAYS[key] - 1)
    return FinanceReportRange(key, start_at.astimezone(timezone.utc), end_at.astimezone(timezone.utc))


def report_intervals(window: FinanceReportRange) -> tuple[str, list[datetime]]:
    duration = window.end - window.start
    if duration <= timedelta(days=2):
        step, label = timedelta(hours=1), "ساعتی"
    elif duration <= timedelta(days=60):
        step, label = timedelta(days=1), "روزانه"
    else:
        step, label = timedelta(days=7), "هفتگی"
    local = window.start.astimezone(TEHRAN)
    boundary = local.replace(minute=0, second=0, microsecond=0)
    if step >= timedelta(days=1):
        boundary = boundary.replace(hour=0)
    boundary += step
    starts = [window.start]
    while boundary.astimezone(timezone.utc) < window.end:
        starts.append(boundary.astimezone(timezone.utc))
        boundary += step
    starts.append(window.end)
    return label, starts


def report_datetime_label(moment: datetime) -> str:
    local = moment.astimezone(TEHRAN)
    return f"{WEEKDAYS_FA[local.weekday()]} {local:%Y/%m/%d · %H:%M}"
