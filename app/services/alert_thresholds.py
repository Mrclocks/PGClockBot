"""When a customer's low-volume and low-time alerts fire.

Each alert has one threshold: a number and the unit it is measured in. Volume is measured in percent
or gigabytes left, time in percent or days left. Only the chosen unit is compared, never both.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.formatting import format_number
from app.services.usage_alerts import SECONDS_PER_DAY, traffic_remaining_pct

UNIT_PERCENT = "pct"
UNIT_GB = "gb"
UNIT_DAY = "day"

TRAFFIC_VALUE_KEY = "user_alert_low_traffic_pct"
TRAFFIC_UNIT_KEY = "user_alert_low_traffic_unit"
TIME_VALUE_KEY = "user_alert_low_time_pct"
TIME_UNIT_KEY = "user_alert_low_time_unit"

TRAFFIC_UNITS: tuple[str, ...] = (UNIT_PERCENT, UNIT_GB)
TIME_UNITS: tuple[str, ...] = (UNIT_PERCENT, UNIT_DAY)
UNIT_LABELS = {UNIT_PERCENT: "درصد", UNIT_GB: "گیگ", UNIT_DAY: "روز"}

DEFAULT_VALUE = 20
VALUE_LIMITS = {UNIT_PERCENT: (1, 99), UNIT_GB: (1, 100_000), UNIT_DAY: (1, 3650)}
BYTES_PER_GB = 1024**3
# A fixed-size threshold is armed again once the remainder is this much above it (percent has its own margin).
ABSOLUTE_REARM_FACTOR = 1.25
PERCENT_REARM_MARGIN = 5.0


@dataclass(frozen=True, slots=True)
class AlertThreshold:
    value: int
    unit: str = UNIT_PERCENT

    @property
    def is_percent(self) -> bool:
        return self.unit == UNIT_PERCENT


def parse_threshold(raw_value: object, raw_unit: object, allowed_units: tuple[str, ...]) -> AlertThreshold:
    """A safe threshold from stored or submitted text: unknown units and bad numbers fall back to defaults."""
    unit = str(raw_unit or "").strip().lower()
    if unit not in allowed_units:
        unit = UNIT_PERCENT
    low, high = VALUE_LIMITS[unit]
    try:
        number = int(float(str(raw_value).strip()))
    except (TypeError, ValueError, OverflowError):
        number = DEFAULT_VALUE
    return AlertThreshold(max(low, min(high, number)), unit)


def traffic_threshold(settings: dict) -> AlertThreshold:
    return parse_threshold(settings.get(TRAFFIC_VALUE_KEY), settings.get(TRAFFIC_UNIT_KEY), TRAFFIC_UNITS)


def time_threshold(settings: dict) -> AlertThreshold:
    return parse_threshold(settings.get(TIME_VALUE_KEY), settings.get(TIME_UNIT_KEY), TIME_UNITS)


def normalize_threshold_form(payload: dict[str, str]) -> None:
    """Clamp submitted threshold values to their unit's range, in place; skips pairs not in the form."""
    for value_key, unit_key, allowed in (
        (TRAFFIC_VALUE_KEY, TRAFFIC_UNIT_KEY, TRAFFIC_UNITS),
        (TIME_VALUE_KEY, TIME_UNIT_KEY, TIME_UNITS),
    ):
        if value_key in payload or unit_key in payload:
            parsed = parse_threshold(payload.get(value_key), payload.get(unit_key), allowed)
            payload[value_key] = str(parsed.value)
            payload[unit_key] = parsed.unit


def _is_low(remaining: float, remaining_pct: float, threshold: AlertThreshold, *, per_unit: float) -> bool:
    if threshold.is_percent:
        return remaining_pct <= threshold.value
    return remaining <= threshold.value * per_unit


def traffic_is_low(used: float, limit: float, threshold: AlertThreshold) -> bool:
    """True when the volume left is at or under the threshold; an unlimited service is never low."""
    pct = traffic_remaining_pct(used, limit)
    if pct is None:
        return False
    return _is_low(max(0.0, limit - used), pct, threshold, per_unit=BYTES_PER_GB)


def time_is_low(remaining_seconds: float, period: float | None, threshold: AlertThreshold) -> bool:
    """True when the time left is at or under the threshold; an ended or unmeasurable period is never low."""
    if remaining_seconds < 0:
        return False
    if threshold.is_percent:
        pct = time_remaining_pct_from_seconds(remaining_seconds, period)
        return pct is not None and pct <= threshold.value
    return remaining_seconds <= threshold.value * SECONDS_PER_DAY


def time_remaining_pct_from_seconds(remaining_seconds: float, period: float | None) -> float | None:
    if not period or period <= 0 or remaining_seconds < 0:
        return None
    return min(100.0, remaining_seconds / period * 100.0)


def should_rearm_traffic(used: float, limit: float, threshold: AlertThreshold) -> bool:
    """True when a volume alert already sent may fire again: the volume left is clearly above the threshold."""
    pct = traffic_remaining_pct(used, limit)
    if pct is None:
        return False
    if threshold.is_percent:
        return pct > threshold.value + PERCENT_REARM_MARGIN
    return max(0.0, limit - used) > threshold.value * BYTES_PER_GB * ABSOLUTE_REARM_FACTOR


def should_rearm_time(remaining_seconds: float, period: float | None, threshold: AlertThreshold) -> bool:
    """True when a time alert already sent may fire again: the time left is clearly above the threshold."""
    if remaining_seconds < 0:
        return False
    if threshold.is_percent:
        pct = time_remaining_pct_from_seconds(remaining_seconds, period)
        return pct is not None and pct > threshold.value + PERCENT_REARM_MARGIN
    return remaining_seconds > threshold.value * SECONDS_PER_DAY * ABSOLUTE_REARM_FACTOR


def describe_threshold(threshold: AlertThreshold) -> str:
    """The threshold as shown to a customer: ``۲۰٪``, ``۵ گیگ`` or ``۳ روز``."""
    shown = format_number(threshold.value)
    return f"{shown}٪" if threshold.is_percent else f"{shown} {UNIT_LABELS[threshold.unit]}"
