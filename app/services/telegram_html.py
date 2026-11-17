"""Make staff-written text safe to send with Telegram's HTML parse mode.

Telegram rejects a whole message when it holds one ``<`` that is not a tag, an unknown tag or an
unbalanced one, so a broadcast with «قیمت < ۱۰۰» failed for every recipient. ``normalize_telegram_html``
keeps the tags Telegram supports, escapes every other ``<``, ``>`` and ``&`` as text, and refuses
tags that are opened and closed out of order, which cannot be repaired without guessing.
"""

from __future__ import annotations

import html
import re

# Tags Telegram's HTML mode accepts (https://core.telegram.org/bots/api#html-style).
ALLOWED_TAGS = frozenset(
    {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "span", "tg-spoiler", "a", "tg-emoji", "code", "pre", "blockquote",
    }
)
# Entities Telegram understands; any other ``&`` is plain text.
NAMED_ENTITIES = frozenset({"lt", "gt", "amp", "quot"})
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*)?)>")
_ENTITY_RE = re.compile(r"&(#\d{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z]{2,8});")
UNBALANCED_MESSAGE = "برچسب‌های HTML متن کامل نیستند (مثلاً <b> باز شده ولی بسته نشده). متن را درست کنید."


def _escape_text(chunk: str) -> str:
    """Escape a run of plain text, keeping the entities Telegram reads."""
    out: list[str] = []
    pos = 0
    for match in _ENTITY_RE.finditer(chunk):
        name = match.group(1)
        keep = name.startswith("#") or name.lower() in NAMED_ENTITIES
        out.append(html.escape(chunk[pos:match.start()], quote=False))
        out.append(match.group(0) if keep else html.escape(match.group(0), quote=False))
        pos = match.end()
    out.append(html.escape(chunk[pos:], quote=False))
    return "".join(out)


def normalize_telegram_html(text: str) -> str:
    """``text`` with supported tags kept and everything else escaped; ``ValueError`` when tags are unbalanced."""
    out: list[str] = []
    open_tags: list[str] = []
    pos = 0
    for match in _TAG_RE.finditer(text or ""):
        closing, name = match.group(1) == "/", match.group(2).lower()
        if name not in ALLOWED_TAGS:
            continue
        out.append(_escape_text(text[pos:match.start()]))
        if closing:
            if not open_tags or open_tags[-1] != name:
                raise ValueError(UNBALANCED_MESSAGE)
            open_tags.pop()
        else:
            open_tags.append(name)
        out.append(match.group(0))
        pos = match.end()
    if open_tags:
        raise ValueError(UNBALANCED_MESSAGE)
    out.append(_escape_text((text or "")[pos:]))
    return "".join(out)
