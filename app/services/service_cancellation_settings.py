"""Settings of the customer's service cancellation, without the cancellation service's own imports.

``users`` reads these to build the settings tabs, so this module imports only ``plan_scope`` and
``reject_reasons``.
The defaults keep the behavior shops had before the settings existed: the request is on for every
plan, an operator approves it, and the remaining share of the payment is only a suggestion.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from app.services.plan_scope import PlanScope, parse_plan_scope
from app.services.reject_reasons import CANCELLATION_REASONS_KEY, MAX_REASONS

ENABLED_KEY = "service_cancel_enabled"
PLANS_KEY = "service_cancel_plans"
APPROVAL_KEY = "service_cancel_approval"
REFUND_KEY = "service_cancel_refund"
FEE_PERCENT_KEY = "service_cancel_fee_percent"
WINDOW_DAYS_KEY = "service_cancel_window_days"
AUTO_MAX_KEY = "service_cancel_auto_max"

APPROVAL_MANUAL = "manual"
APPROVAL_AUTO = "auto"
APPROVAL_CHOICES: tuple[tuple[str, str], ...] = ((APPROVAL_MANUAL, "تأیید دستی ادمین"), (APPROVAL_AUTO, "بازگشت وجه خودکار"))
REFUND_REMAINING = "remaining"
REFUND_FULL = "full"
REFUND_NONE = "none"
REFUND_CHOICES: tuple[tuple[str, str], ...] = (
    (REFUND_REMAINING, "درصد باقی‌مانده"),
    (REFUND_FULL, "کل مبلغ"),
    (REFUND_NONE, "بدون بازگشت"),
)
CHOICES: dict[str, tuple[tuple[str, str], ...]] = {APPROVAL_KEY: APPROVAL_CHOICES, REFUND_KEY: REFUND_CHOICES}

# The same cap the wallet and the catalog use for one amount.
MAX_AMOUNT = 2_000_000_000
CANCEL_DEFAULTS = {
    ENABLED_KEY: "1", PLANS_KEY: "all", APPROVAL_KEY: APPROVAL_MANUAL, REFUND_KEY: REFUND_REMAINING,
    FEE_PERCENT_KEY: "0", WINDOW_DAYS_KEY: "0", AUTO_MAX_KEY: "0",
}
CANCEL_BOUNDS = {FEE_PERCENT_KEY: (0, 100), WINDOW_DAYS_KEY: (0, 365), AUTO_MAX_KEY: (0, MAX_AMOUNT)}
# Stored in ``processed_by`` of a request the policy approved without an operator.
AUTO_ACTOR = "auto"

GROUP_TITLE = "لغو سرویس"
WEB_FIELDS: list[tuple[str, str, str, str]] = [
    (ENABLED_KEY, "لغو سرویس توسط مشتری", "toggle",
     "مشتری از «⚙️ بیشتر» سرویس با نوشتن دلیل درخواست لغو می‌دهد؛ پس از تأیید، سرویس در پنل غیرفعال و "
     "مبلغ برگشتی به کیف پول همین فروشگاه واریز می‌شود."),
    (PLANS_KEY, "پلن‌ها", "plans",
     "فقط سرویس‌هایی که پلنشان این‌جا انتخاب شده لغو می‌شوند. «همهٔ پلن‌ها» پلن‌هایی را هم که بعداً می‌سازید شامل می‌شود."),
    (APPROVAL_KEY, "روش تأیید", "select",
     "دستی: ادمین مبلغ را می‌بیند و تأیید یا رد می‌کند. خودکار: سرویس همان لحظه لغو و مبلغ محاسبه‌شده واریز می‌شود."),
    (REFUND_KEY, "مبلغ برگشتی", "select",
     "درصد باقی‌مانده: سهم زمان یا حجم باقی‌مانده (هر کدام کمتر) از مبلغ دورهٔ فعلی. در تأیید دستی فقط پیشنهاد است."),
    (FEE_PERCENT_KEY, "کارمزد لغو (درصد)", "number", "این درصد از مبلغ برگشتی کم می‌شود. ۰ = بدون کارمزد."),
    (WINDOW_DAYS_KEY, "مهلت لغو پس از خرید یا تمدید (روز)", "number", "بعد از این مدت درخواست لغو پذیرفته نمی‌شود. ۰ = بدون مهلت."),
    (AUTO_MAX_KEY, "سقف بازگشت خودکار (تومان)", "number",
     "در حالت خودکار، مبلغ بیشتر از این سقف برای تأیید ادمین می‌ماند. ۰ = بدون سقف."),
    (CANCELLATION_REASONS_KEY, "دلایل آمادهٔ رد درخواست", "textarea",
     f"هر خط یک دلیل، حداکثر {MAX_REASONS} دلیل. ادمین هنگام رد یکی را انتخاب می‌کند و به مشتری نمایش داده می‌شود."),
]
BOT_FIELDS: list[tuple[str, str, str]] = [(key, label, kind) for key, label, kind, _help in WEB_FIELDS]


@dataclass(frozen=True, slots=True)
class CancellationPolicy:
    enabled: bool
    approval: str
    refund: str
    fee_percent: int
    window_days: int
    auto_max: int
    plans: PlanScope = PlanScope(True, frozenset())

    def allows_plan(self, plan_id: int | None) -> bool:
        return self.enabled and self.plans.allows(plan_id)

    @property
    def is_auto(self) -> bool:
        return self.approval == APPROVAL_AUTO

    def auto_allows(self, amount: int) -> bool:
        """Whether an amount may be paid without an operator; 0 as the cap means no cap."""
        return self.is_auto and (self.auto_max == 0 or amount <= self.auto_max)


def _on(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _bounded(ui: Mapping[str, Any], key: str) -> int:
    low, high = CANCEL_BOUNDS[key]
    try:
        value = int(str(ui.get(key, CANCEL_DEFAULTS[key])).strip())
    except (TypeError, ValueError):
        value = int(CANCEL_DEFAULTS[key])
    return min(max(value, low), high)


def normalize_choice(key: str, value: Any) -> str:
    """A stored choice value; anything outside the choices becomes the default."""
    text = str(value or "").strip().lower()
    allowed = {choice for choice, _label in CHOICES[key]}
    return text if text in allowed else CANCEL_DEFAULTS[key]


def normalize_choices(payload: MutableMapping[str, str]) -> None:
    """Replace submitted choice values that are not one of the offered choices."""
    for key in CHOICES:
        if key in payload:
            payload[key] = normalize_choice(key, payload[key])


def cancellation_policy(ui: Mapping[str, Any]) -> CancellationPolicy:
    return CancellationPolicy(
        enabled=_on(ui.get(ENABLED_KEY, CANCEL_DEFAULTS[ENABLED_KEY])),
        approval=normalize_choice(APPROVAL_KEY, ui.get(APPROVAL_KEY)),
        refund=normalize_choice(REFUND_KEY, ui.get(REFUND_KEY)),
        fee_percent=_bounded(ui, FEE_PERCENT_KEY),
        window_days=_bounded(ui, WINDOW_DAYS_KEY),
        auto_max=_bounded(ui, AUTO_MAX_KEY),
        plans=parse_plan_scope(ui.get(PLANS_KEY, CANCEL_DEFAULTS[PLANS_KEY])),
    )
