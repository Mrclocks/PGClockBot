"""English-only text for the terminal: CLI output, install and update steps, and the service log.

An SSH session often has no Persian font and no right-to-left support, so Persian there is
unreadable. Messages shared with the bot and the web panel stay Persian in the services; whatever
reaches a terminal or the journal passes through here, which drops Persian runs and keeps the
English details (paths, ids, exception names, a tool's own output).
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Arabic-script letters and digits, presentation forms, joiners and direction marks, «».
_PERSIAN_RUN = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿‌-‏«»]+")
_LEFTOVER_PUNCTUATION = re.compile(r"(\s*[—:،؛.,()\-]\s*)+$|^(\s*[—:،؛.,()\-]\s*)+")
_HAS_ENGLISH = re.compile(r"[A-Za-z0-9]")
NO_ENGLISH_DETAIL = "details are only in Persian; see the web panel or: journalctl -u pgclockbot -n 80"
# In a log line the place (logger, level, time) is already English, so a dropped message says only this.
NO_ENGLISH_LOG_DETAIL = "(message text omitted: Persian)"


def has_persian(text: Any) -> bool:
    return bool(_PERSIAN_RUN.search(str(text)))


def terminal_text(text: Any, *, empty_note: str = NO_ENGLISH_DETAIL) -> str:
    """One line without Persian runs; ``empty_note`` when nothing English is left."""
    value = str(text)
    if not _PERSIAN_RUN.search(value):
        return value
    cleaned = " ".join(_PERSIAN_RUN.sub(" ", value).split())
    cleaned = _LEFTOVER_PUNCTUATION.sub("", cleaned).strip()
    return cleaned if _HAS_ENGLISH.search(cleaned) else empty_note


def terminal_lines(text: Any, *, empty_note: str = NO_ENGLISH_DETAIL) -> str:
    """Like ``terminal_text`` for every line, keeping each line's indentation (tracebacks)."""
    value = str(text)
    if not _PERSIAN_RUN.search(value):
        return value
    lines = []
    for line in value.split("\n"):
        if not _PERSIAN_RUN.search(line):
            lines.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines.append(indent + terminal_text(line, empty_note=empty_note))
    return "\n".join(lines)


class EnglishLogFormatter(logging.Formatter):
    """A log formatter whose output never carries Persian (messages, arguments or exception text)."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if not has_persian(message):
            return terminal_lines(super().format(record), empty_note=NO_ENGLISH_LOG_DETAIL)
        # The record is shared with other handlers: swap in the English message only while formatting.
        saved = record.msg, record.args
        record.msg, record.args = terminal_lines(message, empty_note=NO_ENGLISH_LOG_DETAIL), None
        try:
            return terminal_lines(super().format(record), empty_note=NO_ENGLISH_LOG_DETAIL)
        finally:
            record.msg, record.args = saved


def install_english_logging(*, level: int = logging.INFO, fmt: str) -> None:
    """Root logging to stderr with ``EnglishLogFormatter``; libraries that propagate use it too."""
    handler = logging.StreamHandler()
    handler.setFormatter(EnglishLogFormatter(fmt))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
