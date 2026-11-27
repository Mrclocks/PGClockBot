"""Shop maintenance mode: while it is on, new purchases stop in every sales channel.

The bot and the Mini App both ask this module, so a customer cannot buy from the Mini App
what the bot refuses. Renewals, add-ons and support stay open during maintenance.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.services.rich_text import rich_plain_text
from app.services.users import on

MAINTENANCE_ENABLED_KEY = "shop_maintenance_enabled"
MAINTENANCE_TEXT_KEY = "shop_maintenance_text"
FALLBACK_MAINTENANCE_TEXT = "فروشگاه موقتاً در حال به‌روزرسانی است.\nتمدید و پشتیبانی فعال است."
# The Mini App shows server text only up to this length (``errorText`` in core.js).
MINI_APP_TEXT_LIMIT = 200


def maintenance_message(ui: Mapping[str, object]) -> str | None:
    """The shop's maintenance text (rich text as saved) when new purchases are closed, else None."""
    if not on(ui.get(MAINTENANCE_ENABLED_KEY)):
        return None
    raw = ui.get(MAINTENANCE_TEXT_KEY)
    raw_text = raw if isinstance(raw, str) else ""
    return raw_text if rich_plain_text(raw_text).strip() else FALLBACK_MAINTENANCE_TEXT


def maintenance_plain_text(ui: Mapping[str, object], *, limit: int = MINI_APP_TEXT_LIMIT) -> str | None:
    """The maintenance text without formatting, cut to ``limit`` characters, for the Mini App."""
    message = maintenance_message(ui)
    if message is None:
        return None
    plain = rich_plain_text(message).strip() or FALLBACK_MAINTENANCE_TEXT
    return plain[:limit]
