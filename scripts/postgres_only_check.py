#!/usr/bin/env python3
"""SQLite removal ratchet: a changed file must not mention SQLite more than before.

PGClockBot runs on PostgreSQL only. Existing SQLite code and tests are being
removed in stages; until that is finished this check stops new references from
coming in. For every file changed against the merge-base with ``dev`` it
counts case-insensitive ``sqlite`` occurrences and fails when the count grew.
New files start from zero.

Not counted:

* ``alembic/versions/``: merged migrations are never edited.
* ``sqlite_where=`` on partial indexes, which keeps the remaining SQLite tests
  building the same constraints until they move to PostgreSQL.
* Markdown documentation, which may explain the removal.
* This script and its own test.

Usage:
    python scripts/postgres_only_check.py               # compare with the merge-base of dev
    python scripts/postgres_only_check.py upstream/dev  # compare with an explicit ref
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_CANDIDATES = ("upstream/dev", "origin/dev", "dev")
EXEMPT_PREFIXES = (
    "alembic/versions/",
    # The ratchet and its test have to name what they look for.
    "scripts/postgres_only_check.py",
    "tests/test_postgres_only_check.py",
)
# Prose may explain the removal; documentation is cleaned up by hand.
EXEMPT_SUFFIXES = (".md",)
SQLITE_PATTERN = re.compile(r"sqlite", re.IGNORECASE)
TOLERATED_PATTERN = re.compile(r"\bsqlite_where\s*=")


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    base_path: str | None


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def _resolve_base(explicit: str | None) -> str:
    candidates = (explicit,) if explicit else BASE_CANDIDATES
    for ref in candidates:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref], cwd=ROOT, capture_output=True
        )
        if probe.returncode == 0:
            return _git("merge-base", ref, "HEAD").strip()
    raise SystemExit("error: no base ref found; pass one explicitly (e.g. upstream/dev)")


def _changed_files(base: str) -> list[ChangedFile]:
    output = _git("diff", "--name-status", "-M", "--diff-filter=AMR", base, "--")
    changed: list[ChangedFile] = []
    for line in output.splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R"):
            changed.append(ChangedFile(path=paths[1], base_path=paths[0]))
        elif status == "A":
            changed.append(ChangedFile(path=paths[0], base_path=None))
        else:
            changed.append(ChangedFile(path=paths[0], base_path=paths[0]))
    return changed


def count_sqlite_references(content: str) -> int:
    """Count SQLite mentions, leaving out the tolerated partial-index keyword."""
    return len(SQLITE_PATTERN.findall(TOLERATED_PATTERN.sub("", content)))


def _decoded(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _base_count(base: str, base_path: str | None) -> int:
    if base_path is None:
        return 0
    shown = subprocess.run(["git", "show", f"{base}:{base_path}"], cwd=ROOT, capture_output=True)
    if shown.returncode != 0:
        return 0
    content = _decoded(shown.stdout)
    return 0 if content is None else count_sqlite_references(content)


def find_regressions(base: str) -> list[tuple[str, int, int]]:
    """Return ``(path, before, after)`` for every changed file whose count grew."""
    regressions: list[tuple[str, int, int]] = []
    for changed in _changed_files(base):
        if changed.path.startswith(EXEMPT_PREFIXES) or changed.path.endswith(EXEMPT_SUFFIXES):
            continue
        file_path = ROOT / changed.path
        if not file_path.is_file():
            continue
        content = _decoded(file_path.read_bytes())
        if content is None:
            continue
        after = count_sqlite_references(content)
        before = _base_count(base, changed.base_path)
        if after > before:
            regressions.append((changed.path, before, after))
    return regressions


def main(argv: list[str]) -> int:
    base = _resolve_base(argv[0] if argv else None)
    regressions = find_regressions(base)
    for path, before, after in regressions:
        print(f"FAIL {path}: SQLite references {before} -> {after}")
    if regressions:
        print(
            "PostgreSQL is the only supported database. Use tests/postgres_support.py "
            "for database tests instead of SQLite."
        )
        return 1
    print("postgres-only check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
