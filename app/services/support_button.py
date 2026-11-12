"""The «تماس با پشتیبانی» inline button for customer messages that tell the customer to ask for help."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SUPPORT_HOME_CALLBACK = "nv:s:home"
SUPPORT_BUTTON_LABEL = "✉️ تماس با پشتیبانی"


def support_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=SUPPORT_BUTTON_LABEL, callback_data=SUPPORT_HOME_CALLBACK)]


def support_markup() -> InlineKeyboardMarkup:
    """A keyboard with only the support button (opens the support home of the bot the customer is in)."""
    return InlineKeyboardMarkup(inline_keyboard=[support_row()])


def with_support_row(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """``markup`` plus a support row at the bottom, unless it already has the support button."""
    rows = [list(row) for row in markup.inline_keyboard] if markup else []
    if any(b.callback_data == SUPPORT_HOME_CALLBACK for row in rows for b in row):
        return InlineKeyboardMarkup(inline_keyboard=rows)
    rows.append(support_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)
