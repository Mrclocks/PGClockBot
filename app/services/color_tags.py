"""Staff risk-color tags on bot users / resellers — fixed 4-level palette.

Security:
- Tags are staff-only metadata on ``BotUser.color_tag``.
- Callers must enforce shop / descendant scope before mutate.
- Only the four whitelist keys are accepted; free-form colors are rejected.
- Every user has a tag; missing/legacy values normalize to ``green`` (مطمئن).

Levels (merged former color + risk markers):
  green  → مطمئن   (default)
  yellow → مشکوک
  orange → ریسک
  red    → ریسک بالا
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ColorTag:
    key: str
    title_fa: str
    hex: str
    # Short emoji for Telegram bot keyboards (not shown to end users).
    emoji: str


DEFAULT_COLOR_TAG = "green"

# Fixed 4-level risk palette — order is severity ascending.
COLOR_TAGS: tuple[ColorTag, ...] = (
    ColorTag("green", "مطمئن", "#22c55e", "🟢"),
    ColorTag("yellow", "مشکوک", "#eab308", "🟡"),
    ColorTag("orange", "ریسک", "#f97316", "🟠"),
    ColorTag("red", "ریسک بالا", "#ef4444", "🔴"),
)

_BY_KEY = {t.key: t for t in COLOR_TAGS}
VALID_COLOR_KEYS: frozenset[str] = frozenset(_BY_KEY)

# Legacy palette keys from v10/v11 color-only tags → nearest risk level.
_LEGACY_ALIASES: dict[str, str] = {
    "amber": "yellow",
    "blue": DEFAULT_COLOR_TAG,
    "violet": DEFAULT_COLOR_TAG,
    "pink": DEFAULT_COLOR_TAG,
    "slate": DEFAULT_COLOR_TAG,
    # Old filter token for "no tag" — treat as default after unification.
    "_none": DEFAULT_COLOR_TAG,
    "none": DEFAULT_COLOR_TAG,
    "clear": DEFAULT_COLOR_TAG,
    "off": DEFAULT_COLOR_TAG,
}

# Stored values that list filters count as green or yellow (legacy palette and empty).
LEGACY_GREEN_TAGS: tuple[str, ...] = ("blue", "violet", "pink", "slate", "")
LEGACY_YELLOW_TAGS: tuple[str, ...] = ("amber",)

# Kept for tests / callers that still import the name; unused in UI filters.
FILTER_NONE = "_none"


def normalize_color_tag(raw: str | None, *, default: str = DEFAULT_COLOR_TAG) -> str:
    """Return a valid palette key. Never returns None — default is green."""
    if raw is None:
        return default
    key = str(raw).strip().lower()
    if not key or key in {"0", "-", "all"}:
        return default
    if key in _BY_KEY:
        return key
    mapped = _LEGACY_ALIASES.get(key)
    if mapped and mapped in _BY_KEY:
        return mapped
    return default


def normalize_color_filter(raw: str | None) -> str | None:
    """Whitelist list-filter token: palette key, or ``None`` (all).

    ``_none`` is accepted as an alias for green (all users now have a tag).
    """
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if not key or key == "all":
        return None
    if key in _BY_KEY:
        return key
    if key in _LEGACY_ALIASES:
        return _LEGACY_ALIASES[key]
    return None


def color_tag_meta(key: str | None) -> ColorTag:
    """Always returns a ColorTag (defaults to green)."""
    return _BY_KEY[normalize_color_tag(key)]


def effective_color_tag(user: Any) -> str:
    """Stored or default tag for a BotUser-like object."""
    return normalize_color_tag(getattr(user, "color_tag", None))


def color_tags_for_ui() -> list[dict[str, str]]:
    return [
        {
            "key": t.key,
            "title_fa": t.title_fa,
            "hex": t.hex,
            "emoji": t.emoji,
            "label": f"{t.emoji} {t.title_fa}",
        }
        for t in COLOR_TAGS
    ]


def apply_color_tag(user: Any, raw: str | None) -> str:
    """Set ``user.color_tag`` from form/bot input; always stores a valid key."""
    value = normalize_color_tag(raw)
    user.color_tag = value
    return value


def risk_mark(user: Any, *, shop_id: int | None) -> str:
    """The colored circle of the person's risk tag, for Telegram lists and cards.

    The tag is private to the shop that attributed the person (the platform for an unattributed
    one), so any other viewer gets an empty string.
    """
    owner = getattr(user, "reseller_id", None)
    if (int(owner) if owner is not None else None) != (int(shop_id) if shop_id is not None else None):
        return ""
    return color_tag_meta(effective_color_tag(user)).emoji


def tag_mark(tag: str | None) -> str:
    """The colored circle for a tag already read for the viewing shop (see ``customer_marks``)."""
    return color_tag_meta(tag).emoji


def bot_tag_button_label(key: str | None) -> str:
    meta = color_tag_meta(key)
    return f"{meta.emoji} {meta.title_fa}"
