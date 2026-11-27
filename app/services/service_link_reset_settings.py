"""Settings and fixed values of the customer's own subscription link change, without the service's imports.

``users`` reads these to build the settings tabs, so this module imports only ``plan_scope``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.services.plan_scope import PlanScope, parse_plan_scope

ENABLED_KEY = "service_link_reset_enabled"
PER_PERIOD_KEY = "service_link_reset_per_period"
PLANS_KEY = "service_link_reset_plans"
# Off, and on no plan, until a person turns it on and chooses plans.
LINK_RESET_DEFAULTS = {ENABLED_KEY: "0", PLANS_KEY: "", PER_PERIOD_KEY: "2"}
LINK_RESET_BOUNDS = {PER_PERIOD_KEY: (1, 10)}
PERIOD = timedelta(days=30)
# Two changes this close together are a double tap or a mistake, not a leaked link.
COOLDOWN = timedelta(minutes=10)
# A revoke whose answer was lost may still reach the panel a little later; until then an unchanged
# link does not prove it failed.
SETTLE_AFTER = timedelta(minutes=5)
JOB_BATCH = 50

STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
OPEN_STATUSES = (STATUS_PENDING, STATUS_UNKNOWN)

GROUP_TITLE = "تغییر لینک اشتراک"
WEB_FIELDS: list[tuple[str, str, str, str]] = [
    (ENABLED_KEY, "تغییر لینک اشتراک توسط مشتری", "toggle",
     "مشتری از «⚙️ بیشتر» سرویس، لینک اشتراکش را عوض می‌کند؛ لینک قبلی همان لحظه از کار می‌افتد و همهٔ "
     "دستگاه‌ها باید لینک جدید را وارد کنند. برای وقتی است که لینک به دست کس دیگری افتاده."),
    (PLANS_KEY, "پلن‌ها", "plans",
     "فقط سرویس‌هایی که پلنشان این‌جا انتخاب شده لینکشان عوض می‌شود. «همهٔ پلن‌ها» پلن‌هایی را هم که بعداً "
     "می‌سازید شامل می‌شود؛ سرویس آزمایشی هیچ‌وقت شامل نمی‌شود."),
    (PER_PERIOD_KEY, "تعداد تغییر لینک در هر ۳۰ روز", "number", "از ۱ تا ۱۰ بار برای هر سرویس."),
]
BOT_FIELDS: list[tuple[str, str, str]] = [(key, label, kind) for key, label, kind, _help in WEB_FIELDS]


@dataclass(frozen=True, slots=True)
class LinkResetPolicy:
    enabled: bool
    per_period: int
    plans: PlanScope = PlanScope(False, frozenset())

    def allows_plan(self, plan_id: int | None) -> bool:
        return self.enabled and self.plans.allows(plan_id)


def _on(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _bounded(ui: Mapping[str, Any], key: str) -> int:
    low, high = LINK_RESET_BOUNDS[key]
    try:
        value = int(str(ui.get(key, LINK_RESET_DEFAULTS[key])).strip())
    except (TypeError, ValueError):
        value = int(LINK_RESET_DEFAULTS[key])
    return min(max(value, low), high)


def link_reset_policy(ui: Mapping[str, Any]) -> LinkResetPolicy:
    return LinkResetPolicy(
        enabled=_on(ui.get(ENABLED_KEY, "0")),
        per_period=_bounded(ui, PER_PERIOD_KEY),
        plans=parse_plan_scope(ui.get(PLANS_KEY)),
    )
