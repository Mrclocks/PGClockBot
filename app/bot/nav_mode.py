"""Navigation presentation mode: inline-first vs classic dual-keyboard chrome.

``nav_mode`` setting (DEFAULT_SETTINGS):
- ``inline`` (default): one stable main ReplyKeyboard; contextual screens are
  single inline messages (no carrier / chrome filler messages).
- ``classic``: previous behaviour (submenu ReplyKeyboards + chrome carriers).

Missing key on old installs resolves to ``inline``.
"""

from __future__ import annotations

NAV_MODE_INLINE = "inline"
NAV_MODE_CLASSIC = "classic"
NAV_MODE_SETTING = "nav_mode"


def nav_mode(ui: dict | None) -> str:
    raw = str((ui or {}).get(NAV_MODE_SETTING) or NAV_MODE_INLINE).strip().lower()
    if raw == NAV_MODE_CLASSIC:
        return NAV_MODE_CLASSIC
    return NAV_MODE_INLINE


def is_inline_nav(ui: dict | None) -> bool:
    return nav_mode(ui) == NAV_MODE_INLINE
