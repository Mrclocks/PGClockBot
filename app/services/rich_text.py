"""Rich Telegram text for settings (premium / custom emoji via entities).

Web edits store plain/HTML strings. Bot edits may pack text+entities as JSON so
custom emoji survive round-trip when re-sent with entities= (no HTML parse_mode).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from aiogram.types import MessageEntity

logger = logging.getLogger(__name__)

_PACK_VERSION = 1
_RICH_PREFIX = "\x1eRICH1:"  # unlikely in normal settings HTML


def entities_to_json(entities: Sequence[MessageEntity] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entities or []:
        typ = e.type
        typ_s = typ.value if hasattr(typ, "value") else str(typ)
        row: dict[str, Any] = {
            "type": typ_s,
            "offset": int(e.offset),
            "length": int(e.length),
        }
        if e.url:
            row["url"] = e.url
        if e.user and getattr(e.user, "id", None) is not None:
            row["user"] = {"id": int(e.user.id)}
        if e.language:
            row["language"] = e.language
        if e.custom_emoji_id:
            row["custom_emoji_id"] = str(e.custom_emoji_id)
        out.append(row)
    return out


def json_to_entities(rows: list[dict[str, Any]] | None) -> list[MessageEntity]:
    out: list[MessageEntity] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            kwargs: dict[str, Any] = {
                "type": str(row.get("type") or "italic"),
                "offset": int(row["offset"]),
                "length": int(row["length"]),
            }
            if row.get("url"):
                kwargs["url"] = str(row["url"])
            if row.get("language"):
                kwargs["language"] = str(row["language"])
            if row.get("custom_emoji_id"):
                kwargs["custom_emoji_id"] = str(row["custom_emoji_id"])
            out.append(MessageEntity(**kwargs))
        except Exception:
            logger.debug("skip bad entity row %r", row, exc_info=True)
    return out


def pack_rich_text(text: str, entities: Sequence[MessageEntity] | None = None) -> str:
    """Serialize text + entities for Setting.value. No entities → plain text."""
    body = text or ""
    ent = entities_to_json(entities)
    if not ent:
        return body
    payload = json.dumps(
        {"v": _PACK_VERSION, "text": body, "entities": ent},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _RICH_PREFIX + payload


def unpack_rich_text(raw: str | None) -> tuple[str, list[MessageEntity] | None]:
    """Return (text, entities_or_None). Plain/HTML strings → entities None."""
    s = raw if raw is not None else ""
    if not s.startswith(_RICH_PREFIX):
        return s, None
    try:
        data = json.loads(s[len(_RICH_PREFIX) :])
        if not isinstance(data, dict) or int(data.get("v") or 0) != _PACK_VERSION:
            return s, None
        text = str(data.get("text") or "")
        ents = json_to_entities(data.get("entities") or [])
        return text, ents or None
    except Exception:
        logger.debug("rich unpack failed", exc_info=True)
        return s, None


def rich_plain_text(raw: str | None) -> str:
    """Display/edit surface for web textarea (entities stripped to plain text)."""
    text, _ = unpack_rich_text(raw)
    return text


def content_fingerprint(raw: str | None) -> str:
    """Stable hash of visible rules text (ignores entity packing wrapper)."""
    import hashlib

    text, _ = unpack_rich_text(raw)
    normalized = (text or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Setting keys that preserve premium emoji when edited from Telegram.
TERMS_RICH_KEYS = frozenset(
    {
        "terms_entry_text",
        "terms_buy_user_text",
        "terms_buy_reseller_text",
        "terms_entry_btn",
        "terms_buy_user_btn",
        "terms_buy_reseller_btn",
    }
)


def prepare_settings_values_for_web(values: dict) -> dict:
    """Unpack rich keys to plain text for web textareas."""
    out = dict(values)
    for key in TERMS_RICH_KEYS:
        if key in out:
            out[key] = rich_plain_text(out.get(key))
    return out


def merge_rich_settings_on_save(existing: dict, payload: dict) -> dict:
    """Keep packed entities when web save did not change visible text."""
    for key in TERMS_RICH_KEYS:
        if key not in payload:
            continue
        new_plain = payload.get(key) or ""
        old_raw = existing.get(key)
        if rich_plain_text(old_raw) == new_plain:
            payload[key] = old_raw if old_raw is not None else new_plain
    return payload

