"""The bot menu switches, set per bot (platform or shop): auto cleanup, an always-open menu and the restart row."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

KEY_ENABLED = "chat_cleanup_enabled"
KEY_PERSISTENT_MENU = "menu_keyboard_persistent"
KEY_RESTART_BUTTON = "show_restart_button"
_OFF = {"0", "false", "no", "off", ""}


def is_chat_cleanup_on(ui: Mapping[str, Any] | None) -> bool:
    """On unless this bot switched it off."""
    return str((ui or {}).get(KEY_ENABLED) or "").strip().lower() not in {"0", "false", "no", "off"}


def is_menu_keyboard_persistent(ui: Mapping[str, Any] | None) -> bool:
    """Off unless this bot switched it on: the menu comes back by itself after typing."""
    return str((ui or {}).get(KEY_PERSISTENT_MENU) or "").strip().lower() not in _OFF


def chat_cleanup_payload(form: Mapping[str, Any]) -> dict[str, str]:
    """The setting rows a menu settings form saves; an unticked box posts nothing and means off."""
    return {key: "1" if str(form.get(key) or "") == "1" else "0" for key in (KEY_ENABLED, KEY_PERSISTENT_MENU, KEY_RESTART_BUTTON)}
