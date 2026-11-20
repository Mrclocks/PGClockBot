"""Which plans a service may be renewed onto across plan categories.

Plan categories are priced differently. Carrying a service's leftover volume or time into a plan of
another category would move quota bought at one price into a category sold at another. So while a
renewal would carry something over, it stays in the service's own category; a plan of another
category is offered only when it carries nothing: it does not carry that part, or the service has
none of it left. A service without a known plan keeps every choice, as before.

The renewal list decides from the stored quota cache (no panel call; unknown means "has leftover").
The preview and the order decide from the panel's live numbers, the same ones the renewal uses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.services.pasarguard import PasarGuardError
from app.services.renewal_carry import CARRY_TIME, CARRY_VOLUME, plan_carries

CATEGORY_SWITCH_REFUSED = (
    "تا وقتی سرویس حجم یا زمان باقی‌مانده دارد، فقط با پلن‌های دستهٔ خودش تمدید می‌شود؛ "
    "پلن دیگری از همان دسته انتخاب کنید."
)
CATEGORY_CHECK_FAILED = (
    "مانده سرویس الان قابل بررسی نیست؛ کمی بعد دوباره امتحان کنید یا پلنی از دستهٔ خود سرویس انتخاب کنید."
)
# Cached statuses that mean a part is used up.
VOLUME_USED_UP = "limited"
TIME_USED_UP = "expired"
ON_HOLD = "on_hold"


@dataclass(frozen=True, slots=True)
class Leftover:
    """Whether the service may still have volume or time that a renewal could carry."""

    volume: bool
    time: bool


def _category(plan: Any) -> int | None:
    value = getattr(plan, "category_id", None)
    return None if value is None else int(value)


def same_category(current_plan: Any, target_plan: Any) -> bool:
    return _category(current_plan) == _category(target_plan)


def cached_leftover(service: Any, *, now: datetime) -> Leftover:
    """Leftover by the stored quota cache; without a synced cache both parts count as left."""
    if getattr(service, "quota_synced_at", None) is None:
        return Leftover(volume=True, time=True)
    status = str(getattr(service, "quota_status", None) or "")
    # A zero limit is unlimited volume: an unlimited quota carries nothing.
    volume = status != VOLUME_USED_UP and int(getattr(service, "quota_data_limit_bytes", 0) or 0) > 0
    # A timezone-aware column: PostgreSQL returns it with its zone.
    expire = getattr(service, "quota_expire_at", None)
    time_left = status == ON_HOLD or (status != TIME_USED_UP and expire is not None and expire > now)
    return Leftover(volume=volume, time=time_left)


def would_carry(plan: Any, leftover: Leftover) -> bool:
    """Whether renewing onto ``plan`` may carry something, mirroring ``calculate_renewal``."""
    volume = plan_carries(plan, CARRY_VOLUME) and bool(getattr(plan, "data_limit_gb", None)) and leftover.volume
    time = plan_carries(plan, CARRY_TIME) and bool(getattr(plan, "duration_days", None)) and leftover.time
    return volume or time


def offered_plans(plans: list[Any], *, current_plan: Any | None, leftover: Leftover) -> list[Any]:
    """The renewal list: the service's own category, plus other categories' plans that carry nothing."""
    if current_plan is None:
        return list(plans)
    return [p for p in plans if same_category(current_plan, p) or not would_carry(p, leftover)]


def assert_category_switch(current_plan: Any | None, target_plan: Any, renewal: dict) -> None:
    """Refuse another category's plan when the computed renewal carries volume or time over."""
    if current_plan is None or same_category(current_plan, target_plan):
        return
    if int(renewal.get("carried_bytes") or 0) > 0 or int(renewal.get("carried_seconds") or 0) > 0:
        raise ValueError(CATEGORY_SWITCH_REFUSED)


async def check_live_category_switch(
    current_plan: Any | None,
    target_plan: Any,
    terms: dict,
    *,
    load_info: Callable[[], Awaitable[dict]],
    calculate: Callable[[dict, dict], dict],
) -> None:
    """Before a renewal order: the same check on the panel's live numbers; fails closed.

    Only a switch to another category reads the panel; the same category never does.
    """
    if current_plan is None or same_category(current_plan, target_plan):
        return
    try:
        info = await load_info()
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        raise ValueError(CATEGORY_CHECK_FAILED) from exc
    assert_category_switch(current_plan, target_plan, calculate(info, terms))
