"""Rich Telegram text for settings (premium / custom emoji via entities).

Web edits store plain/HTML strings. Bot edits may pack text+entities as JSON so
custom emoji survive round-trip when re-sent with ``entities=`` (no HTML parse_mode).

Message bodies in ``MESSAGE_RICH_KEYS`` are packed and re-sent with entities.
Button labels (``btn_*``) pack custom-emoji entities so keyboards can set Bot API
``icon_custom_emoji_id`` (premium icon before the button text).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Sequence

from aiogram.types import MessageEntity

logger = logging.getLogger(__name__)

_PACK_VERSION = 1
_RICH_PREFIX = "\x1eRICH1:"  # unlikely in normal settings HTML
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


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


def utf16_len(s: str) -> int:
    """Telegram entity offsets are UTF-16 code units."""
    return len((s or "").encode("utf-16-le")) // 2


def clone_entity(e: MessageEntity, *, offset: int | None = None) -> MessageEntity:
    return MessageEntity(
        type=e.type,
        offset=int(e.offset if offset is None else offset),
        length=int(e.length),
        url=e.url,
        user=e.user,
        language=e.language,
        custom_emoji_id=e.custom_emoji_id,
    )


def shift_entities(
    entities: Sequence[MessageEntity] | None, delta_utf16: int
) -> list[MessageEntity] | None:
    if not entities:
        return None if entities is None else []
    if not delta_utf16:
        return [clone_entity(e) for e in entities]
    return [clone_entity(e, offset=int(e.offset) + int(delta_utf16)) for e in entities]


def substitute_preserving_entities(
    text: str,
    entities: Sequence[MessageEntity] | None,
    mapping: Mapping[str, Any] | None,
) -> tuple[str, list[MessageEntity] | None]:
    """Replace ``{key}`` placeholders and keep custom-emoji offsets coherent."""
    body = text or ""
    if not mapping:
        return body, list(entities) if entities else None

    shifts: list[tuple[int, int]] = []
    for match in _PLACEHOLDER_RE.finditer(body):
        key = match.group(1)
        if key not in mapping:
            continue
        old = match.group(0)
        new = "" if mapping[key] is None else str(mapping[key])
        delta = utf16_len(new) - utf16_len(old)
        if delta:
            end_u16 = utf16_len(body[: match.end()])
            shifts.append((end_u16, delta))

    from app.services.safe_format import safe_format

    rendered = safe_format(body, dict(mapping))
    if not entities:
        return rendered, None
    if not shifts:
        return rendered, [clone_entity(e) for e in entities]

    out: list[MessageEntity] = []
    for e in entities:
        off = int(e.offset)
        total = 0
        for end_u16, delta in shifts:
            if off >= end_u16:
                total += delta
        new_off = off + total
        if new_off < 0:
            continue
        out.append(clone_entity(e, offset=new_off))
    return rendered, out or None


def _utf16_slice(text: str, start: int, end: int) -> str:
    """Slice ``text`` by UTF-16 code-unit offsets (Telegram entity space)."""
    if not text or start >= end:
        return ""
    buf = (text or "").encode("utf-16-le")
    return buf[start * 2 : end * 2].decode("utf-16-le", errors="ignore")


def _entity_type_str(e: MessageEntity) -> str:
    typ = e.type
    return typ.value if hasattr(typ, "value") else str(typ)


def first_custom_emoji_id(entities: Sequence[MessageEntity] | None) -> str | None:
    for e in entities or []:
        if _entity_type_str(e) == "custom_emoji" and e.custom_emoji_id:
            return str(e.custom_emoji_id)
    return None


def strip_custom_emoji_spans(
    text: str, entities: Sequence[MessageEntity] | None
) -> str:
    """Remove custom-emoji glyphs so the icon is not duplicated in button text."""
    body = text or ""
    spans = [
        (int(e.offset), int(e.offset) + int(e.length))
        for e in entities or []
        if _entity_type_str(e) == "custom_emoji" and int(e.length) > 0
    ]
    if not spans:
        return body
    spans.sort()
    parts: list[str] = []
    cursor = 0
    total = utf16_len(body)
    for start, end in spans:
        start = max(0, min(start, total))
        end = max(start, min(end, total))
        if start > cursor:
            parts.append(_utf16_slice(body, cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        parts.append(_utf16_slice(body, cursor, total))
    # Collapse leftover double spaces from removed leading icons.
    return " ".join("".join(parts).split())


def is_button_label_key(key: str | None) -> bool:
    """Editable keyboard label settings (not btn_style_* color overrides)."""
    k = str(key or "")
    return k.startswith("btn_") and not k.startswith("btn_style_")


# Reply-action → btn_* setting key (icons look up via the same labels).
_ACTION_TO_BTN_KEY: dict[str, str] = {
    "home": "btn_menu_home",
    "referral": "btn_referral",
    "loy_referral": "btn_referral",
    "topup_card": "btn_pay_card",
    "topup_gateway": "btn_pay_gateway",
    "topup_psp": "btn_pay_psp",
    "topup_crypto": "btn_pay_crypto",
    "svc_renew": "btn_renew",
    "svc_link": "btn_sub_link",
}


def btn_setting_key_for_action(action: str | None) -> str | None:
    a = (action or "").strip()
    if not a:
        return None
    return _ACTION_TO_BTN_KEY.get(a) or f"btn_{a}"


def button_icon_custom_emoji_id(raw: str | None) -> str | None:
    """First packed custom_emoji id for Bot API icon_custom_emoji_id."""
    _, ents = unpack_rich_text(raw)
    return first_custom_emoji_id(ents)


def button_display_text(raw: str | None) -> str:
    """Plain button label: unpack + strip custom-emoji glyphs used as the icon."""
    text, ents = unpack_rich_text(raw)
    if ents and first_custom_emoji_id(ents):
        text = strip_custom_emoji_spans(text, ents)
    return text or ""


def button_icon_from_ui(
    ui: dict | None, *, label_key: str | None = None, action: str | None = None
) -> str | None:
    if not ui:
        return None
    key = label_key or btn_setting_key_for_action(action)
    if not key:
        return None
    return button_icon_custom_emoji_id(ui.get(key))


# Terms gates (kept as a named subset for existing tests / call sites).
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

# Message bodies packed/re-sent with entities. Button labels use icon_custom_emoji_id
# separately (see is_button_label_key) — do not put btn_* here except legacy terms btns.
MESSAGE_RICH_KEYS = frozenset(
    {
        "welcome_text",
        "shop_title",
        "guide_text",
        "faq_text",
        "empty_services_text",
        "shop_empty_text",
        "purchase_success_text",
        "delivery_title",
        "wallet_success_title",
        "wallet_success_text",
        "payment_ok_title",
        "support_text",
        "referral_text",
        "force_join_msg",
        "qr_caption",
        "payment_reject_text",
        "card_pay_text",
        "gateway_pay_text",
        "crypto_pay_text",
        "psp_pay_text",
        "card_auto_hint_text",
        "shop_maintenance_text",
        "admin_daily_report_template",
        "stars_title",
        "stars_description",
        *TERMS_RICH_KEYS,
    }
)


def is_message_rich_key(key: str | None) -> bool:
    return bool(key) and str(key) in MESSAGE_RICH_KEYS


def pack_setting_from_message(key: str, message: Any) -> str:
    """Pack bot-edited setting; preserve entities for message bodies + button icons."""
    text = getattr(message, "text", None) or ""
    entities = getattr(message, "entities", None)
    if is_message_rich_key(key):
        return pack_rich_text(text, entities)
    if is_button_label_key(key) and first_custom_emoji_id(entities):
        return pack_rich_text(text, entities)
    return (text or "").strip()


def _iter_rich_web_keys(values: dict) -> list[str]:
    keys = set(MESSAGE_RICH_KEYS)
    for k in values:
        if is_button_label_key(k):
            keys.add(k)
    return sorted(keys)


def prepare_settings_values_for_web(values: dict) -> dict:
    """Unpack rich keys to plain text for web textareas."""
    out = dict(values)
    for key in _iter_rich_web_keys(out):
        if key in out:
            out[key] = rich_plain_text(out.get(key))
    return out


def merge_rich_settings_on_save(existing: dict, payload: dict) -> dict:
    """Keep packed entities when web save did not change visible text."""
    keys = set(MESSAGE_RICH_KEYS)
    for k in list(payload.keys()) + list(existing.keys()):
        if is_button_label_key(k):
            keys.add(k)
    for key in keys:
        if key not in payload:
            continue
        new_plain = payload.get(key) or ""
        old_raw = existing.get(key)
        if rich_plain_text(old_raw) == new_plain:
            payload[key] = old_raw if old_raw is not None else new_plain
    return payload


def _template_mapping(domain: str, kwargs: Mapping[str, Any]) -> dict[str, str]:
    from app.services.message_variables import allowed_keys_for_domain, _VARS

    allowed = allowed_keys_for_domain(domain)
    merged: dict[str, Any] = dict(kwargs or {})
    for v in _VARS:
        if domain not in v.domains:
            continue
        if v.key in merged:
            for alias in v.aliases:
                if alias in allowed and alias not in merged:
                    merged[alias] = merged[v.key]
        for alias in v.aliases:
            if alias in merged and v.key not in merged and v.key in allowed:
                merged[v.key] = merged[alias]
    out: dict[str, str] = {}
    for key, val in merged.items():
        if key not in allowed:
            continue
        out[key] = "" if val is None else str(val)
    return out


def outbound_setting_text(
    raw: str | None,
    *,
    title: str | None = None,
    title_raw: str | None = None,
    title_prefix: str = "",
    domain: str | None = None,
    append: str | None = None,
    **template_kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Build outbound Telegram text + send kwargs for a stored setting value.

    When custom-emoji entities exist → plain text + ``entities`` + ``parse_mode=None``.
    Otherwise → existing HTML ``format_message`` card (default bot parse_mode).

    ``title_raw`` accepts a packed setting (e.g. ``shop_title``) so premium emoji
    in the title survive; ``title_prefix`` is optional plain text before that title.
    If the title already carries a custom/premium emoji, any prefix is omitted
    so it is not duplicated beside the icon. Prefer putting icons inside
    ``shop_title`` itself rather than hardcoding prefixes at call sites.
    Plain ``title=`` still works for callers without rich titles.
    """
    text, ents = unpack_rich_text(raw)
    if domain:
        mapping = _template_mapping(domain, template_kwargs)
        if ents:
            text, ents = substitute_preserving_entities(text, ents, mapping)
        else:
            from app.services.message_variables import render_message_template

            text = render_message_template(
                text,
                domain=domain,
                html=True,
                **template_kwargs,
            )

    title_ents: list[MessageEntity] | None = None
    if title_raw is not None:
        t_text, title_ents = unpack_rich_text(title_raw)
        # Premium titles already brand themselves — don't also prepend ✨/🛠.
        prefix = ""
        if title_prefix and not first_custom_emoji_id(title_ents):
            prefix = title_prefix
        title_s = f"{prefix}{(t_text or '').strip()}".strip()
        if title_ents and prefix:
            title_ents = shift_entities(title_ents, utf16_len(prefix))
    else:
        title_s = (title or "").strip()

    body = (text or "").strip()
    if ents or title_ents:
        merged: list[MessageEntity] = []
        if title_s:
            prefix = f"{title_s}\n━━━━━━━━━━━━\n"
            if title_ents:
                merged.extend(title_ents)
            if ents:
                merged.extend(shift_entities(ents, utf16_len(prefix)) or [])
            body = prefix + body
        elif ents:
            merged.extend(ents)
        if append:
            body = body + (append if append.startswith("\n") else f"\n{append}")
        return body, {"entities": merged, "parse_mode": None}

    from app.services.formatting import format_message

    if title_s:
        out = format_message(title_s, body)
    else:
        out = body
    if append:
        out = out + (append if append.startswith("\n") else f"\n{append}")
    return out, {}
