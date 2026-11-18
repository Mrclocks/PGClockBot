"""The «تنظیمات بکاپ» button of the backup panel and the callbacks it links."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from app.bot.keyboards import _ikb, _t

BACKUP_PANEL_CALLBACK = "nv:adm:backup"
BACKUP_SETTINGS_CALLBACK = "adm:st:sec:backup"
LABEL_KEY = "btn_backup_settings"


def backup_settings_button(ui: dict | None) -> InlineKeyboardButton:
    """Opens the backup settings section of the owner bot settings."""
    return _ikb(
        _t(ui, LABEL_KEY),
        callback_data=BACKUP_SETTINGS_CALLBACK,
        ui=ui,
        label_key=LABEL_KEY,
        style_id="backup_settings",
    )
