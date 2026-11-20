"""Settings and fixed texts of the customer's service freeze, without the service's own imports.

``users`` reads these to build the settings tabs, so this module imports only ``plan_scope``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.services.plan_scope import ALL, PlanScope, parse_plan_scope

ENABLED_KEY = "service_freeze_enabled"
MAX_DAYS_KEY = "service_freeze_max_days"
PER_PERIOD_KEY = "service_freeze_per_period"
MIN_DAYS_LEFT_KEY = "service_freeze_min_days_left"
PLANS_KEY = "service_freeze_plans"
FREEZE_DEFAULTS = {ENABLED_KEY: "0", PLANS_KEY: ALL, MAX_DAYS_KEY: "14", PER_PERIOD_KEY: "1", MIN_DAYS_LEFT_KEY: "1"}
FREEZE_BOUNDS = {MAX_DAYS_KEY: (1, 90), PER_PERIOD_KEY: (1, 10), MIN_DAYS_LEFT_KEY: (0, 30)}
PERIOD = timedelta(days=30)
STATUS_PAUSING = "pausing"
STATUS_FROZEN = "frozen"
STATUS_RESUMING = "resuming"
STATUS_DONE = "done"
# The service was cancelled or removed while frozen: the freeze ends without touching the panel.
STATUS_CLOSED = "closed"
OPEN_STATUSES = (STATUS_PAUSING, STATUS_FROZEN, STATUS_RESUMING)
BY_CUSTOMER = "customer"
BY_DEADLINE = "deadline"
JOB_BATCH = 50
# Cleanup must not delete a frozen service whose old expiry passes while it is paused.
CLEANUP_HOLD_MARGIN = timedelta(days=2)
PRESET_DAYS = (3, 7, 14, 30)
RESUMED_ACTION = "service_freeze_resumed"
RESUMED_TEXT = (
    "▶️ <b>سرویس شما دوباره فعال شد</b>\n"
    "سرویس: <code>{name}</code>\n"
    "مهلت توقف تمام شد و روزهای باقی‌ماندهٔ سرویس از همین حالا ادامه پیدا می‌کند."
)

GROUP_TITLE = "توقف موقت سرویس"
WEB_FIELDS: list[tuple[str, str, str, str]] = [
    (ENABLED_KEY, "توقف موقت سرویس توسط مشتری", "toggle",
     "مشتری از «⚙️ بیشتر» سرویس، آن را چند روز متوقف می‌کند؛ روزهای باقی‌مانده حفظ می‌شود و با «ادامهٔ سرویس» "
     "یا در پایان مهلت، سرویس با همان روزها دوباره فعال می‌شود."),
    (PLANS_KEY, "پلن‌هایی که توقف دارند", "plans",
     "فقط سرویس‌هایی که پلنشان این‌جا انتخاب شده می‌توانند متوقف شوند. «همهٔ پلن‌ها» پلن‌هایی را هم که بعداً می‌سازید شامل می‌شود."),
    (MAX_DAYS_KEY, "حداکثر روز توقف", "number", "از ۱ تا ۹۰ روز. در پایان این مهلت سرویس خودکار فعال می‌شود."),
    (PER_PERIOD_KEY, "تعداد توقف در هر ۳۰ روز", "number", "هر سرویس در هر ۳۰ روز چند بار می‌تواند متوقف شود."),
    (MIN_DAYS_LEFT_KEY, "حداقل روز باقی‌مانده", "number",
     "سرویسی که کمتر از این روز مانده دارد متوقف نمی‌شود. ۰ = بدون حداقل."),
]
BOT_FIELDS: list[tuple[str, str, str]] = [(key, label, kind) for key, label, kind, _help in WEB_FIELDS]
# The other fields show only while the switch is on (users.SETTING_VISIBILITY).
FREEZE_VISIBILITY = {key: (ENABLED_KEY, ("1",)) for key, *_rest in WEB_FIELDS if key != ENABLED_KEY}


@dataclass(frozen=True, slots=True)
class FreezePolicy:
    enabled: bool
    max_days: int
    per_period: int
    min_days_left: int
    plans: PlanScope = PlanScope(True, frozenset())

    def allows_plan(self, plan_id: int | None) -> bool:
        return self.enabled and self.plans.allows(plan_id)

    def day_choices(self) -> list[int]:
        return sorted({d for d in PRESET_DAYS if d < self.max_days} | {self.max_days})


def _bounded(ui: Mapping[str, Any], key: str) -> int:
    low, high = FREEZE_BOUNDS[key]
    try:
        value = int(str(ui.get(key, FREEZE_DEFAULTS[key])).strip())
    except (TypeError, ValueError):
        value = int(FREEZE_DEFAULTS[key])
    return min(max(value, low), high)


def freeze_policy(ui: Mapping[str, Any]) -> FreezePolicy:
    enabled = str(ui.get(ENABLED_KEY, "0")).strip().lower() in {"1", "true", "on", "yes"}
    return FreezePolicy(
        enabled, _bounded(ui, MAX_DAYS_KEY), _bounded(ui, PER_PERIOD_KEY), _bounded(ui, MIN_DAYS_LEFT_KEY),
        parse_plan_scope(ui.get(PLANS_KEY)),
    )
