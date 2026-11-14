"""Numbers as the web panel shows them: Persian digits, the Persian thousands separator «٬» and decimal mark «٫».

The shared formatters in ``formatting.py`` also feed Telegram texts and keep their ASCII digits; the panel's template
filters wrap them here so every figure on a page reads the same way (DESIGN_SYSTEM.md §2). Only display text goes
through these: form values, links and data attributes read by scripts keep ASCII digits.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.services.formatting import (
    format_bytes,
    format_bytes_ratio,
    format_expire_short,
    format_gb,
    format_money,
    format_number,
)
from app.services.numbers import to_fa_digits

# A point between two digits is a decimal mark; a point anywhere else (end of a sentence, a version) is left alone.
_DECIMAL_POINT = re.compile(r"(?<=[۰-۹])\.(?=[۰-۹])")


def persian_display(formatter: Callable[..., str]) -> Callable[..., str]:
    """``formatter`` with its result's digits, thousands separators and decimal marks in Persian."""

    def display(*args: Any, **kwargs: Any) -> str:
        return _DECIMAL_POINT.sub("٫", to_fa_digits(formatter(*args, **kwargs)).replace(",", "٬"))

    display.__name__ = f"fa_{formatter.__name__}"
    display.__doc__ = formatter.__doc__
    return display


WEB_NUMBER_FILTERS: dict[str, Callable[..., str]] = {
    "num": persian_display(format_number),
    "money": persian_display(format_money),
    "gb": persian_display(format_gb),
    "bytes": persian_display(format_bytes),
    "bytes_ratio": persian_display(format_bytes_ratio),
    "expire": persian_display(format_expire_short),
    "fa": to_fa_digits,
}
