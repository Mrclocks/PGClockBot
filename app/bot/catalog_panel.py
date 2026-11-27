"""The shop's lists inside the menu editor's panel: categories or plans with ⬆️/⬇️, and columns.

A row's name opens the category or plan in the catalog row editor (``edr:<a|r>:c|p:<id>``),
its arrows move it (``edr:<a|r>:m:<list>:<from>:<to>:<tag>``, see ``catalog_row_editor``),
and the columns row sets the list's columns (``edl:<a|r>:w:<screen>:<0|1|2>``).
"""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton

from app.bot.edit_mode import CATALOG_ROW_ROOT, MENU_ROWS_ROOT
from app.services.catalog_order import CatalogList

BTN_UP = "⬆️"
BTN_DOWN = "⬇️"
# Stands where an arrow would lead nowhere (⬆️ on the first row, ⬇️ on the last).
BTN_NO_MOVE = "➖"
COLUMN_LABELS = (("0", "خودکار"), ("1", "۱ ستون"), ("2", "۲ ستون"))
ITEM_ICONS = {"c": "🏷", "p": "💎"}
TEXT_LIST_HINT = "فهرست «{title}»: با ⬆️ و ⬇️ ترتیب را عوض کنید (همان «ترتیب» در وب‌پنل)؛ روی نام بزنید تا نام یا رنگش عوض شود."
TEXT_LIST_TRUNCATED = "فقط {count} مورد اول این‌جا آمده؛ ترتیب بقیه را از وب‌پنل تنظیم کنید."


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _arrow(code: str, screen_id: str, catalog: CatalogList, text: str, index: int, target: int) -> InlineKeyboardButton:
    if not 0 <= target < len(catalog.items):
        return _button(BTN_NO_MOVE, f"{MENU_ROWS_ROOT}:{code}:e:{screen_id}")
    return _button(text, f"{CATALOG_ROW_ROOT}:{code}:m:{catalog.token}:{index}:{target}:{catalog.state}")


def catalog_rows(code: str, screen_id: str, catalog: CatalogList) -> list[list[InlineKeyboardButton]]:
    """One row per category or plan: the name between ⬆️ and ⬇️ (➖ where it cannot move)."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(catalog.items):
        name = _button(f"{ITEM_ICONS[item.kind]} {item.name}", f"{CATALOG_ROW_ROOT}:{code}:{item.kind}:{item.id}")
        if len(catalog.items) < 2:
            rows.append([name])
            continue
        rows.append([
            _arrow(code, screen_id, catalog, BTN_UP, index, index - 1),
            name,
            _arrow(code, screen_id, catalog, BTN_DOWN, index, index + 1),
        ])
    return rows


def columns_row(code: str, screen_id: str, current: int | None) -> list[InlineKeyboardButton]:
    chosen = str(current or 0)
    return [
        _button(f"✓ {label}" if value == chosen else label, f"{MENU_ROWS_ROOT}:{code}:w:{screen_id}:{value}")
        for value, label in COLUMN_LABELS
    ]


def catalog_lines(catalog: CatalogList) -> list[str]:
    if not catalog.items:
        return []
    lines = [TEXT_LIST_HINT.format(title=html.escape(catalog.title))]
    if catalog.truncated:
        lines.append(TEXT_LIST_TRUNCATED.format(count=len(catalog.items)))
    return lines
