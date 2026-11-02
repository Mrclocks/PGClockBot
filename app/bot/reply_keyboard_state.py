"""Telling a tap on a menu button from text the person typed.

A tap on a reply-keyboard button sends exactly that button's label, never with a premium emoji.
"""

from __future__ import annotations

from aiogram.types import Message

from app.bot import keyboards as kb
from app.services.rich_text import first_custom_emoji_id, is_button_label_key
from app.services.users import DEFAULT_SETTINGS


def visible_button_labels(ui: dict[str, str]) -> set[str]:
    """Texts a menu button can send when tapped, with this bot's settings."""
    keys = {key for key in (*DEFAULT_SETTINGS, *ui) if is_button_label_key(key)}
    labels = {kb._t(ui, key).strip() for key in keys}
    labels |= {kb.BTN_BACK, kb.BTN_RESTART, kb._home_label(ui).strip(), kb._back_label(ui).strip()}
    labels.discard("")
    return labels


def is_menu_tap(message: Message, ui: dict[str, str]) -> bool:
    """A tap on a menu button sends exactly that button's text and never a premium emoji."""
    text = (message.text or "").strip()
    if not text or first_custom_emoji_id(message.entities):
        return False
    return text in visible_button_labels(ui)
