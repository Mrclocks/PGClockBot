"""The note stored on a panel user when an order is delivered.

It lets a panel admin trace a user back to the order and to the Telegram account that bought it.
"""
from __future__ import annotations

import re

_TELEGRAM_USERNAME_CHARS = re.compile(r"[^A-Za-z0-9_]")
MAX_USERNAME_LENGTH = 32


def build_panel_user_note(
    *, order_id: int, telegram_id: int | None, telegram_username: str | None, index: int = 1, quantity: int = 1
) -> str:
    """``PGClockBot order #12 (1/3) | id: 123456 | @name``; the username is cleaned because customers choose it."""
    parts = [f"PGClockBot order #{int(order_id)}" + (f" ({int(index)}/{int(quantity)})" if quantity > 1 else "")]
    if telegram_id:
        parts.append(f"id: {int(telegram_id)}")
    cleaned = _TELEGRAM_USERNAME_CHARS.sub("", str(telegram_username or ""))[:MAX_USERNAME_LENGTH]
    if cleaned:
        parts.append(f"@{cleaned}")
    return " | ".join(parts)
