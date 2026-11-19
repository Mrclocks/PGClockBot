"""CLI output helpers — clear, non-noisy status lines.

Everything printed to the terminal is English. An SSH session often has no Persian font and no
right-to-left support, so Persian text there is unreadable; shared services still return Persian
messages for the bot and the web panel, and ``terminal_text`` (``services.terminal_text``) keeps
them off the terminal.
"""

from __future__ import annotations

import sys
from typing import Any

from app.services.terminal_text import NO_ENGLISH_DETAIL, terminal_text

__all__ = ["NO_ENGLISH_DETAIL", "CliError", "err", "header", "human_size", "info", "kv", "ok", "terminal_text", "warn"]

SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")
SIZE_STEP = 1024


class CliError(Exception):
    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def human_size(num_bytes: int | float | None) -> str:
    """English binary size for the terminal: «512 B», «12.3 MB»."""
    try:
        size = abs(float(num_bytes or 0))
    except (TypeError, ValueError):
        return "?"
    unit = 0
    while size >= SIZE_STEP and unit < len(SIZE_UNITS) - 1:
        size /= SIZE_STEP
        unit += 1
    return f"{int(size)} {SIZE_UNITS[unit]}" if unit == 0 else f"{size:.1f} {SIZE_UNITS[unit]}"


def _stream(msg: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    stream.write(terminal_text(msg) + "\n")
    stream.flush()


def info(msg: str) -> None:
    _stream(f"  · {msg}")


def ok(msg: str) -> None:
    _stream(f"  ✓ {msg}")


def warn(msg: str) -> None:
    _stream(f"  ! {msg}", err=True)


def err(msg: str) -> None:
    _stream(f"  ✗ {msg}", err=True)


def header(title: str) -> None:
    _stream("")
    _stream(f"PGClock · {title}")
    _stream("─" * (10 + len(title)))


def kv(key: str, value: Any) -> None:
    _stream(f"  {key:<16} {value}")
