"""Settings and fixed texts of the customer's service transfer, without the service's own imports.

``users`` reads these to build the settings tabs, so this module imports only ``plan_scope``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.services.plan_scope import PlanScope, parse_plan_scope

ENABLED_KEY = "service_transfer_enabled"
LINK_HOURS_KEY = "service_transfer_link_hours"
PER_PERIOD_KEY = "service_transfer_per_period"
NEW_LINK_KEY = "service_transfer_new_link"
PLANS_KEY = "service_transfer_plans"
# Off, and on no plan, until a person turns it on and chooses plans.
TRANSFER_DEFAULTS = {ENABLED_KEY: "0", PLANS_KEY: "", LINK_HOURS_KEY: "24", PER_PERIOD_KEY: "1", NEW_LINK_KEY: "1"}
TRANSFER_BOUNDS = {LINK_HOURS_KEY: (1, 168), PER_PERIOD_KEY: (1, 10)}
PERIOD = timedelta(days=30)
STATUS_OPEN = "open"
STATUS_ACCEPTED = "accepted"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
DEEPLINK_PREFIX = "tr_"
DONE_ACTION = "service_transfer_done"
DONE_TEXT = (
    "🔁 <b>سرویس شما منتقل شد</b>\n"
    "سرویس: <code>{name}</code>\n"
    "گیرنده لینک انتقال را پذیرفت و این سرویس دیگر در «سرویس‌های من» شما نیست."
)

GROUP_TITLE = "انتقال سرویس"
WEB_FIELDS: list[tuple[str, str, str, str]] = [
    (ENABLED_KEY, "انتقال سرویس به مشتری دیگر", "toggle",
     "مشتری از «⚙️ بیشتر» سرویس یک لینک یک‌بارمصرف می‌سازد؛ کسی که آن را در همین ربات باز کند و بپذیرد صاحب سرویس "
     "می‌شود. تمدید خودکار سرویس خاموش می‌شود تا از کیف پول کسی بدون رضایتش برداشت نشود."),
    (PLANS_KEY, "پلن‌ها", "plans",
     "فقط سرویس‌هایی که پلنشان این‌جا انتخاب شده منتقل می‌شوند؛ هنگام پذیرفتن لینک هم دوباره بررسی می‌شود. "
     "«همهٔ پلن‌ها» پلن‌هایی را هم که بعداً می‌سازید شامل می‌شود."),
    (LINK_HOURS_KEY, "اعتبار لینک انتقال (ساعت)", "number", "از ۱ تا ۱۶۸ ساعت؛ بعد از آن لینک کار نمی‌کند."),
    (PER_PERIOD_KEY, "تعداد انتقال در هر ۳۰ روز", "number", "هر سرویس در هر ۳۰ روز چند بار می‌تواند منتقل شود."),
    (NEW_LINK_KEY, "لینک اشتراک بعد از انتقال عوض شود", "toggle",
     "پیشنهادی: روشن. لینک اشتراک قبلی از کار می‌افتد تا صاحب قبلی دیگر نتواند از سرویس استفاده کند."),
]
BOT_FIELDS: list[tuple[str, str, str]] = [(key, label, kind) for key, label, kind, _help in WEB_FIELDS]



@dataclass(frozen=True, slots=True)
class TransferPolicy:
    enabled: bool
    link_hours: int
    per_period: int
    new_link: bool
    plans: PlanScope = PlanScope(False, frozenset())

    def allows_plan(self, plan_id: int | None) -> bool:
        return self.enabled and self.plans.allows(plan_id)


def _on(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _bounded(ui: Mapping[str, Any], key: str) -> int:
    low, high = TRANSFER_BOUNDS[key]
    try:
        value = int(str(ui.get(key, TRANSFER_DEFAULTS[key])).strip())
    except (TypeError, ValueError):
        value = int(TRANSFER_DEFAULTS[key])
    return min(max(value, low), high)


def transfer_policy(ui: Mapping[str, Any]) -> TransferPolicy:
    return TransferPolicy(
        enabled=_on(ui.get(ENABLED_KEY, "0")),
        link_hours=_bounded(ui, LINK_HOURS_KEY),
        per_period=_bounded(ui, PER_PERIOD_KEY),
        new_link=_on(ui.get(NEW_LINK_KEY, "1")),
        plans=parse_plan_scope(ui.get(PLANS_KEY)),
    )
