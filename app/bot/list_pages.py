"""Pages for long button lists: ``<list callback>:p:<n>`` opens page n (0-based).

A list callback without the suffix is page 0, so buttons on older messages keep working.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from aiogram.types import InlineKeyboardButton

from app.services.numbers import to_fa_digits

PAGE_SUFFIX = ":p:"
PREVIOUS_TEXT = "◀️ قبلی"
NEXT_TEXT = "بعدی ▶️"

# A TypeVar, not ``class Page[T]``: the installer also runs the panel on Python 3.11.
T = TypeVar("T")

@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: list[T]
    index: int
    count: int
    total: int

    @property
    def is_split(self) -> bool:
        return self.count > 1


def page_number(data: str | None) -> int:
    """The page a callback asks for; 0 for a plain list callback or a malformed suffix."""
    _base, sep, raw = (data or "").rpartition(PAGE_SUFFIX)
    if not sep or not raw.isdigit():
        return 0
    return int(raw)


def page_callback(base: str, index: int) -> str:
    return base if index <= 0 else f"{base}{PAGE_SUFFIX}{index}"


def page_of(items: Sequence[T], index: int, size: int) -> Page[T]:
    """Page ``index`` of ``items``; an index past the end shows the last page."""
    total = len(items)
    count = max(1, -(-total // size))
    index = min(max(0, index), count - 1)
    start = index * size
    return Page(items=list(items[start : start + size]), index=index, count=count, total=total)


def pager_row(page: Page, base: str) -> list[InlineKeyboardButton]:
    """«قبلی» / «بعدی» for a split list; empty when everything fits on one page."""
    row: list[InlineKeyboardButton] = []
    if page.index > 0:
        row.append(InlineKeyboardButton(text=PREVIOUS_TEXT, callback_data=page_callback(base, page.index - 1)))
    if page.index + 1 < page.count:
        row.append(InlineKeyboardButton(text=NEXT_TEXT, callback_data=page_callback(base, page.index + 1)))
    return row


def page_line(page: Page) -> str:
    """Message-text line «صفحه ۲ از ۵ · ۴۷ مورد» for a split list, else empty."""
    if not page.is_split:
        return ""
    return (
        f"\n\nصفحه {to_fa_digits(page.index + 1)} از {to_fa_digits(page.count)}"
        f" · {to_fa_digits(page.total)} مورد"
    )
