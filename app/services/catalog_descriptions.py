"""Customer-facing text block listing the descriptions of shop categories and plans."""
from __future__ import annotations

import html
from collections.abc import Iterable

from app.services.rich_text import premium_text_html

DESCRIPTION_LIMIT = 300
BLOCK_LIMIT = 2500


def description_html(item: object, *, limit: int | None = DESCRIPTION_LIMIT) -> str:
    """The item's description as Telegram HTML, with its premium emoji when edited in the bot."""
    text = str(getattr(item, "description", None) or "").strip()
    packed = getattr(item, "description_rich", None)
    if limit is None or len(text) <= limit:
        return premium_text_html(text, packed)
    cut = len(text[: limit - 1].rstrip())
    return premium_text_html(text, packed, end=cut) + "…"


def describe_items(items: Iterable[object], *, label: str | None = None) -> str:
    """HTML lines «name — description» for items that have a description.

    Returns an empty string when no item has one, so callers can append it unconditionally.
    ``label`` overrides the item's own name (used for category button labels).
    """
    lines: list[str] = []
    size = 0
    for item in items:
        description = str(getattr(item, "description", None) or "").strip()
        name = str(label or getattr(item, "name", "") or "").strip()
        if not description or not name:
            continue
        line = f"<b>{html.escape(name)}</b>\n{description_html(item)}"
        size += len(line)
        if size > BLOCK_LIMIT:
            break
        lines.append(line)
    return "\n\n".join(lines)


def with_descriptions(body: str, items: Iterable[object]) -> str:
    """``body`` followed by the items' descriptions, when any exist."""
    block = describe_items(items)
    return f"{body}\n\n{block}" if block else body
