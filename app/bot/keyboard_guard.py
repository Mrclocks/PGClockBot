"""Which reply keyboards may reach a chat: only the main menu keyboards.

The bot has one bottom keyboard, the main menu of the person's role (customer, Owner, shop
staff, or the customer preview). Its builders register every keyboard they produce here; any
other reply keyboard, such as a leftover submenu, is never sent (see ``app.bot.chat_screen``).
The one exception is the phone-number step, which needs a «share contact» button for one step.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from aiogram.types import ReplyKeyboardMarkup

# Distinct main keyboards seen by this process (roles × settings × service ownership).
REMEMBERED_KEYBOARDS = 4096

_main: OrderedDict[str, None] = OrderedDict()


def keyboard_fingerprint(markup: ReplyKeyboardMarkup) -> str:
    """The buttons as the person sees them, and whether the menu stays open; not the placeholder."""
    rows = [[button.text for button in row] for row in markup.keyboard]
    return json.dumps([rows, bool(markup.is_persistent)], ensure_ascii=False, separators=(",", ":"))


def register_main_keyboard(markup: ReplyKeyboardMarkup) -> ReplyKeyboardMarkup:
    """Mark ``markup`` as a main menu keyboard and return it unchanged."""
    fingerprint = keyboard_fingerprint(markup)
    _main[fingerprint] = None
    _main.move_to_end(fingerprint)
    while len(_main) > REMEMBERED_KEYBOARDS:
        _main.popitem(last=False)
    return markup


def is_main_keyboard(markup: ReplyKeyboardMarkup) -> bool:
    return keyboard_fingerprint(markup) in _main


def asks_for_contact(markup: ReplyKeyboardMarkup) -> bool:
    """A one-step keyboard asking for the phone number: Telegram offers no other way to share it."""
    return any(getattr(button, "request_contact", False) for row in markup.keyboard for button in row)
