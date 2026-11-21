#!/usr/bin/env python3
"""PostgreSQL-only check: no tracked file may mention SQLite.

PGClockBot runs on PostgreSQL only and its SQLite code is gone. This check
scans every file tracked by git for case-insensitive ``sqlite`` and fails on
any hit, so SQLite code, tests or settings cannot come back.

Not scanned:

* ``alembic/versions/``: merged migrations are never edited, and some still
  carry SQLite branches that PostgreSQL never takes.
* Markdown, which may describe the history.
* This script and its own test.

Usage:
    python scripts/postgres_only_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXEMPT_PREFIXES = (
    "alembic/versions/",
    # The check and its test have to name what they look for.
    "scripts/postgres_only_check.py",
    "tests/test_postgres_only_check.py",
)
# Prose may describe the history; documentation is cleaned up by hand.
EXEMPT_SUFFIXES = (".md",)
SQLITE_PATTERN = re.compile(r"sqlite", re.IGNORECASE)


def count_sqlite_references(content: str) -> int:
    """Count SQLite mentions in one file's text."""
    return len(SQLITE_PATTERN.findall(content))


def _tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8")
    return [path for path in output.split("\0") if path]


def _is_exempt(path: str) -> bool:
    return path.startswith(EXEMPT_PREFIXES) or path.endswith(EXEMPT_SUFFIXES)


def find_references() -> list[tuple[str, int]]:
    """Return ``(path, count)`` for every scanned tracked file that mentions SQLite."""
    found: list[tuple[str, int]] = []
    for path in _tracked_files():
        file_path = ROOT / path
        if _is_exempt(path) or not file_path.is_file():
            continue
        try:
            content = file_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        count = count_sqlite_references(content)
        if count:
            found.append((path, count))
    return found


def main() -> int:
    found = find_references()
    for path, count in found:
        print(f"FAIL {path}: {count} SQLite reference(s)")
    if found:
        print(
            "PostgreSQL is the only supported database. Use tests/postgres_support.py "
            "for database tests instead of SQLite."
        )
        return 1
    print("postgres-only check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
