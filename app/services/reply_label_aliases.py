"""Keep stale ReplyKeyboard labels working after btn_* text edits.

Telegram clients keep the last ReplyKeyboardMarkup until a new one is sent.
Navigation resolves taps by exact label match against *current* settings — so
renaming a button silently breaks users who still see the old text.

We persist previous display strings (per reply action) and also treat drifted
DEFAULT_SETTINGS labels as aliases so edits made before this feature still heal.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.retired_text_defaults import retired_labels
from app.services.rich_text import (
    _ACTION_TO_BTN_KEY,
    button_display_text,
    is_button_label_key,
    rich_plain_text,
)

log = logging.getLogger(__name__)

ALIAS_SETTING_KEY = "reply_btn_label_aliases"
MAX_ALIASES_PER_ACTION = 12

# Labels the code shipped as defaults before they were reworded. Customers keep
# the old keyboard until the bot sends a new one, so these must keep resolving
# wherever the button is still enabled. Append when changing a btn_* default.
RETIRED_DEFAULT_LABELS: dict[str, tuple[str, ...]] = {
    "btn_connection_status": ("📡 وضعیت از دید سرور",),
    "btn_connection_status_admin": ("📡 تست و وضعیت اتصال",),
}

# Reverse of special action→btn_* map (btn key may map to several actions).
_BTN_KEY_TO_ACTIONS: dict[str, list[str]] = {}
for _action, _btn in _ACTION_TO_BTN_KEY.items():
    _BTN_KEY_TO_ACTIONS.setdefault(_btn, []).append(_action)


def actions_for_btn_setting_key(btn_key: str) -> list[str]:
    """Reply-action keys that use this btn_* setting for their label."""
    if not is_button_label_key(btn_key):
        return []
    actions = list(_BTN_KEY_TO_ACTIONS.get(btn_key) or [])
    fallback = btn_key[4:]  # btn_shop → shop
    if fallback and fallback not in actions:
        actions.append(fallback)
    return actions


def label_variants(raw: str | None) -> list[str]:
    """Distinct plain labels a keyboard might show for a stored btn value."""
    out: list[str] = []
    for variant in (
        button_display_text(raw).strip(),
        rich_plain_text(raw).strip(),
    ):
        if variant and variant not in out:
            out.append(variant)
    return out


def parse_aliases(raw: str | None) -> dict[str, list[str]]:
    if not (raw or "").strip():
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for action, labels in data.items():
        a = str(action or "").strip()
        if not a or not isinstance(labels, list):
            continue
        cleaned: list[str] = []
        for lab in labels:
            t = str(lab or "").strip()
            if t and t not in cleaned:
                cleaned.append(t)
            if len(cleaned) >= MAX_ALIASES_PER_ACTION:
                break
        if cleaned:
            out[a] = cleaned
    return out


def serialize_aliases(data: dict[str, list[str]]) -> str:
    slim: dict[str, list[str]] = {}
    for action, labels in (data or {}).items():
        a = str(action or "").strip()
        if not a:
            continue
        cleaned: list[str] = []
        for lab in labels or []:
            t = str(lab or "").strip()
            if t and t not in cleaned:
                cleaned.append(t)
            if len(cleaned) >= MAX_ALIASES_PER_ACTION:
                break
        if cleaned:
            slim[a] = cleaned
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def record_btn_label_change(
    aliases: dict[str, list[str]],
    *,
    btn_key: str,
    old_raw: str | None,
    new_raw: str | None,
) -> dict[str, list[str]]:
    """Return updated aliases after a btn_* value change."""
    if not is_button_label_key(btn_key):
        return aliases
    old_labels = label_variants(old_raw)
    new_labels = set(label_variants(new_raw))
    if not old_labels:
        return aliases
    # No visible change — keep aliases as-is.
    if old_labels == label_variants(new_raw):
        return aliases
    out = {k: list(v) for k, v in aliases.items()}
    for action in actions_for_btn_setting_key(btn_key):
        lst = list(out.get(action) or [])
        for old in old_labels:
            if old in new_labels:
                continue
            if old in lst:
                lst.remove(old)
            lst.insert(0, old)
        # Drop aliases that match the new live label(s).
        lst = [x for x in lst if x not in new_labels]
        if lst:
            out[action] = lst[:MAX_ALIASES_PER_ACTION]
        else:
            out.pop(action, None)
    return out


def merge_alias_updates_for_payload(
    *,
    existing_ui: dict[str, Any],
    payload: dict[str, str],
) -> dict[str, str] | None:
    """If payload changes btn_* keys, return {ALIAS_SETTING_KEY: serialized} or None."""
    btn_changes = {
        k: v for k, v in payload.items() if is_button_label_key(k) and k != ALIAS_SETTING_KEY
    }
    if not btn_changes:
        return None
    aliases = parse_aliases(existing_ui.get(ALIAS_SETTING_KEY))
    # Allow explicit alias payload to be the starting point when present.
    if ALIAS_SETTING_KEY in payload:
        aliases = parse_aliases(payload.get(ALIAS_SETTING_KEY))
    before = serialize_aliases(aliases)
    for key, new_val in btn_changes.items():
        old_val = existing_ui.get(key)
        aliases = record_btn_label_change(
            aliases, btn_key=key, old_raw=old_val, new_raw=new_val
        )
    after = serialize_aliases(aliases)
    if after == before and ALIAS_SETTING_KEY not in payload:
        return None
    return {ALIAS_SETTING_KEY: after}


def register_aliases_on_mapping(mapping: dict[str, str], ui: dict | None) -> None:
    """setdefault stale labels → action (current labels already in mapping win).

    Only actions already present in ``mapping`` are extended — never introduce
    admin/reseller actions onto a user keyboard map via aliases.
    """
    ui = ui or {}
    allowed_actions = {str(v).strip() for v in mapping.values() if str(v).strip()}
    if not allowed_actions:
        return
    aliases = parse_aliases(ui.get(ALIAS_SETTING_KEY))

    # Heal edits made before alias persistence existed: default text still maps.
    try:
        from app.services.users import DEFAULT_SETTINGS
    except Exception:
        DEFAULT_SETTINGS = {}

    for btn_key, default_raw in DEFAULT_SETTINGS.items():
        if not is_button_label_key(btn_key):
            continue
        current_labels = set(label_variants(ui.get(btn_key, default_raw)))
        default_labels = [
            *label_variants(default_raw),
            *RETIRED_DEFAULT_LABELS.get(btn_key, ()),
            *retired_labels(btn_key),
        ]
        for default_label in default_labels:
            if not default_label or default_label in current_labels:
                continue
            for action in actions_for_btn_setting_key(btn_key):
                if action not in allowed_actions:
                    continue
                aliases.setdefault(action, [])
                if default_label not in aliases[action]:
                    aliases[action].append(default_label)

    for action, labels in aliases.items():
        a = str(action or "").strip()
        if not a or a not in allowed_actions:
            continue
        for lab in labels:
            t = (lab or "").strip()
            if t:
                mapping.setdefault(t, a)
