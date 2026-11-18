"""Volume of the current period: what a customer bought with the last renewal, not since creation.

PasarGuard keeps one lifetime ``used_traffic``; a renewal writes ``data_limit = used + new
remaining`` in one absolute write instead of resetting usage (a reset is a second, non-atomic
call and needs its own panel permission). Shown raw, a fresh 50 GB renewal reads «98.5 از
148.5» and the low-volume alert fires earlier with every renewal. The renewal therefore stores
the usage at period start on the service, and display and alerts subtract it.

The stored start is only trusted while the panel numbers still fit it: usage at least the
start, a limit above it, and, when the panel reports ``lifetime_used_traffic``, both counters
grown by the same amount (a usage reset in the panel breaks that). Otherwise the raw numbers
are shown, which is what the bot showed before.
"""

from __future__ import annotations

from typing import Any

from app.db.models import UserService

# Counters are written together by the panel; allow small drift between the two reads.
COUNTER_DRIFT_BYTES = 64 * 1024**2


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def trusted_period_start(service: UserService | None, *, used: Any, limit: Any, lifetime: Any = None) -> int | None:
    """The stored usage at period start when it still matches the panel numbers, else None."""
    if service is None:
        return None
    start = _int(getattr(service, "period_used_baseline_bytes", None))
    used_n, limit_n = _int(used), _int(limit)
    if not start or used_n is None or not limit_n or used_n < start or limit_n <= start:
        return None
    lifetime_n = _int(lifetime)
    lifetime_start = _int(getattr(service, "period_lifetime_baseline_bytes", None))
    if lifetime_n is not None and lifetime_start is not None:
        if abs((lifetime_n - lifetime_start) - (used_n - start)) > COUNTER_DRIFT_BYTES:
            return None
    return start


def period_usage(service: UserService | None, used: float, limit: float, lifetime: Any = None) -> tuple[float, float]:
    """``(used, limit)`` of the current period; the raw pair when no trusted start exists."""
    start = trusted_period_start(service, used=used, limit=limit, lifetime=lifetime)
    if start is None:
        return used, limit
    return used - start, limit - start


def period_view(info: dict[str, Any], service: UserService | None) -> dict[str, Any]:
    """A copy of panel ``info`` with volume of the current period, for display only.

    Never pass the result to code that writes the panel or computes a renewal: those need
    the raw lifetime numbers.
    """
    if not isinstance(info, dict) or "used_traffic" not in info or not info.get("data_limit"):
        return info
    start = trusted_period_start(
        service, used=info.get("used_traffic"), limit=info.get("data_limit"),
        lifetime=info.get("lifetime_used_traffic"),
    )
    if start is None:
        return info
    view = dict(info)
    view["used_traffic"] = int(info["used_traffic"]) - start
    view["data_limit"] = int(info["data_limit"]) - start
    return view


def record_period_start(service: UserService, *, used: Any, lifetime: Any, finite_limit: bool) -> None:
    """Remember where the period a renewal just opened starts (cleared for unlimited volume)."""
    used_n = _int(used)
    if not finite_limit or not used_n:
        clear_period_start(service)
        return
    service.period_used_baseline_bytes = used_n
    service.period_lifetime_baseline_bytes = _int(lifetime)


def clear_period_start(service: UserService) -> None:
    """Usage was reset (or the volume is unlimited): raw panel numbers are the period again."""
    service.period_used_baseline_bytes = None
    service.period_lifetime_baseline_bytes = None
