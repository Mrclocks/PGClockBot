"""Plans grouped by category the way the shop bot shows them.

When a shop has categories, the bot's first purchase step is one button per category, in category order, and a
«سایر» button for plans without one. A category only gets a button while it is on and has at least one plan that is
on (``shop_categories_for_menu``). The plans page groups its list the same way, so the panel shows the bot's
structure and says when a category is missing from the bot and why.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.db.models import Plan, PlanCategory
from app.services.button_styles import resolve_category_button_style
from app.services.plan_categories import AUDIENCE_USERS, normalize_audience

# Telegram button style -> panel tone, so a group wears the colour of its bot button.
STYLE_TONES = {"primary": "primary", "success": "success", "danger": "danger"}
DEFAULT_TONE = "default"
OTHER_BUTTON_LABEL = "سایر"
FIXED_BUTTON_FALLBACK = "ثابت"
NOTE_CATEGORY_OFF = "خاموش است؛ در ربات دیده نمی‌شود"
NOTE_NO_PLANS = "پلنی ندارد؛ در ربات دیده نمی‌شود"
NOTE_NO_ACTIVE_PLANS = "پلن روشنی ندارد؛ در ربات دیده نمی‌شود"


@dataclass(frozen=True, slots=True)
class CategoryGroup:
    category: PlanCategory
    plans: list[Plan]
    tone: str
    # Why the category has no button in the bot; None while it has one.
    bot_note: str | None

    @property
    def in_bot(self) -> bool:
        return self.bot_note is None


@dataclass(frozen=True, slots=True)
class PlanBoard:
    groups: list[CategoryGroup] = field(default_factory=list)
    other_plans: list[Plan] = field(default_factory=list)
    other_title: str = ""
    other_note: str = ""

    @property
    def has_categories(self) -> bool:
        return bool(self.groups)


def category_tone(ui: Mapping[str, object] | None, category: PlanCategory) -> str:
    style = resolve_category_button_style(dict(ui or {}), category)
    return STYLE_TONES.get(str(style or ""), DEFAULT_TONE)


def category_tones(categories: Sequence[PlanCategory], ui: Mapping[str, object] | None = None) -> dict[int, str]:
    """Tone of every user-audience category, for the coloured dots of the category chips in plan forms."""
    return {
        int(c.id): category_tone(ui, c) for c in categories if normalize_audience(c.audience) == AUDIENCE_USERS
    }


def _bot_note(category: PlanCategory, plans: Sequence[Plan]) -> str | None:
    if not category.is_active:
        return NOTE_CATEGORY_OFF
    if not plans:
        return NOTE_NO_PLANS
    if not any(plan.is_active for plan in plans):
        return NOTE_NO_ACTIVE_PLANS
    return None


def _other_heading(*, any_in_bot: bool, fixed_label: str) -> tuple[str, str]:
    if any_in_bot:
        return OTHER_BUTTON_LABEL, f"بدون دسته؛ در ربات زیر دکمهٔ «{OTHER_BUTTON_LABEL}» می‌آید"
    return "بدون دسته", f"هنوز دسته‌ای در ربات دیده نمی‌شود؛ همهٔ پلن‌ها زیر دکمهٔ «{fixed_label}» می‌آیند"


def build_plan_board(
    plans: Sequence[Plan],
    categories: Sequence[PlanCategory],
    *,
    ui: Mapping[str, object] | None = None,
) -> PlanBoard:
    """Group sale plans under their user-audience categories, in the bot's order.

    ``categories`` must already be the staff shop's own; plans keep their given order inside each group. A plan
    whose category is not one of them (another audience, or gone) is listed with the plans that have none.
    """
    user_categories = sorted(
        (c for c in categories if normalize_audience(c.audience) == AUDIENCE_USERS),
        key=lambda c: (c.sort_order or 0, c.id),
    )
    if not user_categories:
        return PlanBoard(other_plans=list(plans))
    by_category: dict[int, list[Plan]] = {int(c.id): [] for c in user_categories}
    other_plans: list[Plan] = []
    for plan in plans:
        bucket = by_category.get(int(plan.category_id)) if plan.category_id is not None else None
        (bucket if bucket is not None else other_plans).append(plan)
    groups = [
        CategoryGroup(
            category=c,
            plans=by_category[int(c.id)],
            tone=category_tone(ui, c),
            bot_note=_bot_note(c, by_category[int(c.id)]),
        )
        for c in user_categories
    ]
    fixed_label = str((ui or {}).get("btn_shop_kind_fixed") or "").strip() or FIXED_BUTTON_FALLBACK
    title, note = _other_heading(any_in_bot=any(g.in_bot for g in groups), fixed_label=fixed_label)
    return PlanBoard(groups=groups, other_plans=other_plans, other_title=title, other_note=note)
