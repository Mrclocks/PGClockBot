"""Premium (custom) emoji icons on staff-named buttons stored as JSON list items.

Staff type a support contact title, a guide title or a guide link label in the bot. When
the message carries a premium emoji, the first one becomes the button's Bot API
``icon_custom_emoji_id`` and its glyph is removed from the stored label, the same way
plan categories keep their icon. The web panel cannot send premium emoji, so its saves
keep the icon already stored for the same item.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from aiogram.types import MessageEntity

from app.services.rich_text import first_custom_emoji_id, strip_custom_emoji_spans

ICON_KEY = "icon_custom_emoji_id"

PREMIUM_ICON_HINT = "اگر اموجی پریمیوم بفرستید، آیکون دکمه می‌شود."

# Telegram custom emoji ids are decimal document ids.
_ICON_ID_RE = re.compile(r"^\d{1,32}$")


def clean_icon_id(raw: Any) -> str | None:
    """A stored or submitted icon id, or ``None`` when it is missing or malformed."""
    if isinstance(raw, bool) or raw is None:
        return None
    value = str(raw).strip()
    return value if _ICON_ID_RE.match(value) else None


def split_label_and_icon(
    text: str | None, entities: Sequence[MessageEntity] | None
) -> tuple[str, str | None]:
    """Label without premium glyphs, and the first premium emoji id of a Telegram message.

    A message made only of premium emoji keeps its text as the label, so the button is
    never left without one.
    """
    raw = (text or "").strip()
    if not raw:
        return "", None
    icon = clean_icon_id(first_custom_emoji_id(entities))
    if not icon:
        return raw, None
    # Entity offsets count from the unstripped text.
    label = strip_custom_emoji_spans(text or "", entities).strip()
    return (label or raw), icon


def set_icon(item: dict[str, Any], icon: Any) -> dict[str, Any]:
    """Store ``icon`` on ``item`` (in place), or drop the key when there is none."""
    clean = clean_icon_id(icon)
    if clean:
        item[ICON_KEY] = clean
    else:
        item.pop(ICON_KEY, None)
    return item


def icon_of(item: dict[str, Any] | None) -> str | None:
    return clean_icon_id((item or {}).get(ICON_KEY))


def icon_kwargs(item: dict[str, Any] | None) -> dict[str, str]:
    """``InlineKeyboardButton`` keyword for the item's icon (empty when it has none)."""
    icon = icon_of(item)
    return {ICON_KEY: icon} if icon else {}


def keep_stored_icons(
    submitted: list[dict[str, Any]], stored: list[dict[str, Any]], *, key: str = "id"
) -> list[dict[str, Any]]:
    """Give each submitted item the icon stored for the same ``key``.

    The stored icon wins; a valid submitted one (an imported bundle) is kept only when
    nothing is stored for that item.
    """
    stored_icons = {str(s.get(key)): icon_of(s) for s in stored if s.get(key) is not None}
    out = []
    for item in submitted:
        entry = dict(item)
        set_icon(entry, stored_icons.get(str(item.get(key))) or item.get(ICON_KEY))
        out.append(entry)
    return out
