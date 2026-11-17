"""Customer-facing text block listing the descriptions of shop categories and plans."""
from __future__ import annotations

import html
from collections.abc import Iterable

DESCRIPTION_LIMIT = 300
BLOCK_LIMIT = 2500


def _clip(text: str) -> str:
    return text if len(text) <= DESCRIPTION_LIMIT else text[: DESCRIPTION_LIMIT - 1].rstrip() + "…"


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
        line = f"<b>{html.escape(name)}</b>\n{html.escape(_clip(description))}"
        size += len(line)
        if size > BLOCK_LIMIT:
            break
        lines.append(line)
    return "\n\n".join(lines)


def with_descriptions(body: str, items: Iterable[object]) -> str:
    """``body`` followed by the items' descriptions, when any exist."""
    block = describe_items(items)
    return f"{body}\n\n{block}" if block else body
