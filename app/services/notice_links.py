"""Buttons under a staff notice that open a screen in a panel of their own.

Tapping «👤 کاربر» under a notice used to edit the notice itself into the customer card, so the
notice was gone and «بازگشت» led to the users list. A notice button that opens a read-only screen
now carries ``nto:<screen callback>``; the bot opens that screen in a new message under the notice
and the notice stays as it was. Buttons that act (approve, reject, reply) are left unchanged.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

NOTICE_OPEN_PREFIX = "nto:"
NOTICE_CLOSE_CALLBACK = "ntc"
# Telegram refuses callback data longer than 64 bytes.
CALLBACK_DATA_LIMIT = 64

# Read-only screens a notice may open. Each handler still checks the actor and the shop.
NOTICE_SCREEN_PREFIXES: tuple[str, ...] = (
    "adm:users:view:",
    "adm:users:svc:",
    "adm:order:",
    "adm:resellers:view:",
    "res:user:",
    "cnrv:v:",
    "payvoid:v:",
)


def is_notice_screen(data: str | None) -> bool:
    return bool(data) and str(data).startswith(NOTICE_SCREEN_PREFIXES)


def notice_open_data(data: str) -> str:
    """The callback a notice button carries for ``data``; unchanged when it is not a screen."""
    wrapped = f"{NOTICE_OPEN_PREFIX}{data}"
    if not is_notice_screen(data) or len(wrapped.encode()) > CALLBACK_DATA_LIMIT:
        return data
    return wrapped


def opened_screen(data: str | None) -> str | None:
    """The screen callback inside a notice button's data, or None when it is not one we sent."""
    if not data or not data.startswith(NOTICE_OPEN_PREFIX):
        return None
    screen = data[len(NOTICE_OPEN_PREFIX):]
    return screen if is_notice_screen(screen) else None


def notice_markup(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """``markup`` with its screen buttons opening a new panel; other buttons are kept as they are."""
    if markup is None:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    changed = False
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            data = button.callback_data
            wrapped = notice_open_data(data) if data else data
            if wrapped != data:
                button = button.model_copy(update={"callback_data": wrapped})
                changed = True
            new_row.append(button)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup
