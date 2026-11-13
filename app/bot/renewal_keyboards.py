"""Keyboards of the renewal screen: the current plan, the shop's categories and a category's plans."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import keyboards as kb
from app.config import get_settings
from app.services.button_styles import resolve_category_button_style
from app.services.catalog_pricing import sale_price
from app.services.formatting import format_toman
from app.services.service_renewals import RENEWAL_LABELS, RenewalChoices

OTHER_CATEGORY_LABEL = "سایر"
OTHER_CATEGORY_CODE = "none"


def renewal_button_text(plan: object, relation: str | None) -> str:
    label = f"{RENEWAL_LABELS[relation]} · " if relation else ""
    return f"{label}{plan.name} — {format_toman(sale_price(plan), get_settings().currency)}"


def _plan_button(service_id: int, plan: object, choices: RenewalChoices, categories: dict) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=renewal_button_text(plan, choices.relations.get(int(plan.id))),
        callback_data=f"svc:renewpay:{int(service_id)}:{int(plan.id)}",
        icon_custom_emoji_id=kb.category_icon_for(plan, categories),
    )


def renewal_keyboard(
    service_id: int, choices: RenewalChoices, ui: dict, categories: dict
) -> InlineKeyboardMarkup:
    """The current plan first; then the categories, or every plan when the shop has none."""
    if not choices.categories:
        rows = [[_plan_button(service_id, p, choices, categories)] for p in choices.options]
    else:
        rows = [[_plan_button(service_id, choices.current, choices, categories)]] if choices.current else []
        rows += [
            [kb._ikb(
                kb._category_button_label(category),
                callback_data=f"svc:renewcat:{int(service_id)}:{int(category.id)}",
                style=resolve_category_button_style(ui, category),
                icon_custom_emoji_id=getattr(category, "icon_custom_emoji_id", None),
            )]
            for category in choices.categories
        ]
        if choices.uncategorized:
            rows.append([kb._ikb(
                OTHER_CATEGORY_LABEL,
                callback_data=f"svc:renewcat:{int(service_id)}:{OTHER_CATEGORY_CODE}",
                style_id="shop_kind_fixed",
                style_fallback="primary",
                ui=ui,
            )])
    rows.append([kb.back_button(ui, f"svc:view:{int(service_id)}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renewal_category_keyboard(
    service_id: int, plans: list, choices: RenewalChoices, ui: dict, categories: dict
) -> InlineKeyboardMarkup:
    rows = [[_plan_button(service_id, p, choices, categories)] for p in plans]
    rows.append([kb.back_button(ui, f"svc:renew:{int(service_id)}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
