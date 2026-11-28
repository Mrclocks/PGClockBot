"""Which plans a service may be renewed onto: only the plans of its own plan category.

The category is the product the customer bought (volume-based, unlimited, ...). A renewal keeps it:
the renewal list shows only that category's plans and an order for another category's plan is
refused, whatever would or would not carry over. A service whose own category has no active plan
left has nothing to renew onto and is bought again from the shop. A service without a known plan
keeps every choice.
"""

from __future__ import annotations

from typing import Any

# When nothing in the service's category can be renewed onto (at most 200 characters: a Telegram alert).
RENEWAL_UNAVAILABLE = (
    "امکان تمدید این سرویس وجود ندارد، چون پلن‌های دستهٔ آن دیگر برای تمدید عرضه نمی‌شوند. "
    "می‌توانید از «خرید اشتراک» یک سرویس جدید تهیه کنید."
)
OWN_CATEGORY_ONLY = "این سرویس فقط با پلن‌های دستهٔ خودش تمدید می‌شود؛ پلنی از همان دسته انتخاب کنید."


def _category(plan: Any) -> int | None:
    value = getattr(plan, "category_id", None)
    return None if value is None else int(value)


def same_category(current_plan: Any, target_plan: Any) -> bool:
    return _category(current_plan) == _category(target_plan)


def offered_plans(plans: list[Any], *, current_plan: Any | None) -> list[Any]:
    """The renewal list: the plans of the service's own category (every plan without a known plan)."""
    if current_plan is None:
        return list(plans)
    return [p for p in plans if same_category(current_plan, p)]


def refuse_other_category(current_plan: Any | None, target_plan: Any) -> None:
    """Refuse renewing onto a plan of another category."""
    if current_plan is not None and not same_category(current_plan, target_plan):
        raise ValueError(OWN_CATEGORY_ONLY)
