"""Bot settings screen for a plan-scope field: «همهٔ پلن‌ها» or the shop's plans one by one."""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import keyboards as kb
from app.services.plan_scope import ALL, PlanChoice, parse_plan_scope, scope_summary

FIELD_KIND = "plans"
PICKER_HINT = (
    "روی هر پلن بزنید تا روشن یا خاموش شود. «همهٔ پلن‌ها» پلن‌هایی را هم که بعداً می‌سازید شامل می‌شود."
)


def scope_button(prefix: str, key: str, label: str, raw: str | None) -> InlineKeyboardButton:
    """The field row: label and current choice; opens the picker."""
    return InlineKeyboardButton(text=f"🗂 {label}: {scope_summary(raw)}", callback_data=f"{prefix}:plo:{key}")


def picker_text(label: str) -> str:
    return f"🗂 <b>{html.escape(label)}</b>\n{PICKER_HINT}"


def picker_markup(prefix: str, key: str, raw: str | None, choices: list[PlanChoice], back_cb: str) -> InlineKeyboardMarkup:
    scope = parse_plan_scope(raw)
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if scope.all_plans else '⬜️'} همهٔ پلن‌ها", callback_data=f"{prefix}:pls:{key}:{ALL}",
    )]]
    for choice in choices:
        mark = "✅" if scope.all_plans or choice.plan_id in scope.plan_ids else "⬜️"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {choice.name}"[:64], callback_data=f"{prefix}:pls:{key}:{choice.plan_id}",
        )])
    rows.append([kb.back_button(None, back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
