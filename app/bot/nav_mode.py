"""Bot navigation is inline-only (Option B).

ReplyKeyboard = thin level-0 anchors (shop / wallet / support / … + staff entry).
All nested menus are InlineKeyboardMarkup on one edited panel message.

``nav_mode`` / classic dual-keyboard chrome has been removed. Helpers below
remain as thin compatibility shims so older call sites keep compiling until
fully purged; they always report inline.
"""

from __future__ import annotations

NAV_MODE_INLINE = "inline"
NAV_MODE_SETTING = "nav_mode"


def nav_mode(ui: dict | None = None) -> str:
    _ = ui
    return NAV_MODE_INLINE


def is_inline_nav(ui: dict | None = None) -> bool:
    _ = ui
    return True
