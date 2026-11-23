"""Count the SQL statements one unit of work runs: one panel request or one bot button tap.

The recorder listens on SQLAlchemy engines. A statement is counted only while a tally is active in
the current context (``contextvars``), so a background job running at the same time is not counted
against the request or tap being measured, while tasks the measured code starts are.

Statements are grouped by their shape: whitespace collapsed and every bound parameter, including an
expanded ``IN`` list, written as ``?``. The same shape run many times in one unit of work is how an
N+1 query shows up.
"""

from __future__ import annotations

import contextvars
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import greenlet
from sqlalchemy import event

# The same statement shape this many times in one unit of work is reported as a likely N+1.
REPEAT_THRESHOLD = 5
# How many of a unit's most frequent statements are kept, to see which one grows with the data.
TOP_SHAPES = 8
# Shapes are compared whole (two selects of one table share their first hundred characters) and
# cut to this length only where they are shown.
SHOWN_SHAPE_LENGTH = 240

_PARAM_LIST = re.compile(r"\$\d+(?:\s*,\s*\$\d+)*|%\(\w+\)s(?:\s*,\s*%\(\w+\)s)*|\?(?:\s*,\s*\?)*")
_NUMBER = re.compile(r"(?<![\w$])\d+(?![\w])")
_SPACE = re.compile(r"\s+")


def statement_shape(sql: str) -> str:
    """The statement with parameters, numbers and spacing normalized."""
    shape = _SPACE.sub(" ", sql).strip()
    shape = _PARAM_LIST.sub("?", shape)
    shape = _NUMBER.sub("N", shape)
    return shape


@dataclass(slots=True)
class Tally:
    """What one unit of work ran."""

    label: str
    statements: int = 0
    db_seconds: float = 0.0
    shapes: Counter[str] = field(default_factory=Counter)
    # Where each shape first ran: the innermost app line outside the database layer.
    origins: dict[str, str] = field(default_factory=dict)

    def repeated(self, threshold: int = REPEAT_THRESHOLD) -> list[tuple[str, int]]:
        """Statement shapes run at least ``threshold`` times, most frequent first."""
        return [(shape, count) for shape, count in self.shapes.most_common() if count >= threshold]

    def _entry(self, shape: str, count: int) -> dict[str, Any]:
        return {"shape": shape[:SHOWN_SHAPE_LENGTH], "count": count, "origin": self.origins.get(shape, "")}

    def as_dict(self) -> dict[str, Any]:
        top = self.shapes.most_common(1)
        return {
            "label": self.label,
            "statements": self.statements,
            "db_ms": round(self.db_seconds * 1000, 1),
            "max_repeat": top[0][1] if top else 0,
            "repeated": [self._entry(shape, count) for shape, count in self.repeated()],
            "top": [self._entry(shape, count) for shape, count in self.shapes.most_common(TOP_SHAPES)],
        }


_current: contextvars.ContextVar[Tally | None] = contextvars.ContextVar("query_tally", default=None)
_START_KEY = "query_counter_started"
_APP_MARKER = "/app/"
_DB_LAYER = "/app/db/"


def _app_origin() -> str:
    """``app/services/x.py:120 in load`` for the innermost app frame that ran the statement.

    Async SQLAlchemy runs the driver call in a greenlet of its own; the awaiting app code is then on
    the stack of the greenlet that started it.
    """
    stacks = [sys._getframe(2)]
    current = greenlet.getcurrent()
    while current.parent is not None:
        current = current.parent
        stacks.append(current.gr_frame)
    for frame in stacks:
        while frame is not None:
            path = frame.f_code.co_filename.replace("\\", "/")
            if _APP_MARKER in path and _DB_LAYER not in path:
                relative = path[path.rindex(_APP_MARKER) + 1:]
                return f"{relative}:{frame.f_lineno} in {frame.f_code.co_name}"
            frame = frame.f_back
    return ""


def _before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    if _current.get() is not None:
        conn.info.setdefault(_START_KEY, []).append(time.perf_counter())


def _after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    tally = _current.get()
    if tally is None:
        return
    started = conn.info.get(_START_KEY)
    if started:
        tally.db_seconds += time.perf_counter() - started.pop()
    tally.statements += 1
    shape = statement_shape(statement)
    tally.shapes[shape] += 1
    if shape not in tally.origins:
        tally.origins[shape] = _app_origin()


class QueryRecorder:
    """Listens on the given engines (sync or async) until ``close``."""

    def __init__(self, *engines: Any) -> None:
        self._engines: list[Any] = []
        for engine in engines:
            self.attach(engine)

    def attach(self, engine: Any) -> None:
        sync_engine = getattr(engine, "sync_engine", engine)
        if any(existing is sync_engine for existing in self._engines):
            return
        event.listen(sync_engine, "before_cursor_execute", _before)
        event.listen(sync_engine, "after_cursor_execute", _after)
        self._engines.append(sync_engine)

    def close(self) -> None:
        for sync_engine in self._engines:
            event.remove(sync_engine, "before_cursor_execute", _before)
            event.remove(sync_engine, "after_cursor_execute", _after)
        self._engines.clear()

    @staticmethod
    @contextmanager
    def measure(label: str) -> Iterator[Tally]:
        """Count the statements run inside the block (and in tasks it starts) as ``label``."""
        tally = Tally(label)
        token = _current.set(tally)
        try:
            yield tally
        finally:
            _current.reset(token)


def markdown_table(rows: list[dict[str, Any]], *, title: str, limit: int = 40) -> str:
    """The heaviest units of work first: statements, database time and the most repeated shape."""
    ordered = sorted(rows, key=lambda row: (row["statements"], row["db_ms"]), reverse=True)[:limit]
    lines = [f"### {title}", "", "| Unit | Runs | Statements (max) | DB ms (max) | Most repeated |", "|---|---|---|---|---|"]
    for row in ordered:
        repeated = row.get("repeated") or []
        worst = f"{repeated[0]['count']}× `{repeated[0]['shape'][:70]}`" if repeated else ""
        lines.append(f"| `{row['label']}` | {row.get('runs', 1)} | {row['statements']} | {row['db_ms']} | {worst} |")
    return "\n".join(lines) + "\n"


def merge_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per label, keeping the heaviest run and counting how many there were."""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        kept = merged.get(row["label"])
        runs = (kept or {}).get("runs", 0) + 1
        if kept is None or (row["statements"], row["db_ms"]) > (kept["statements"], kept["db_ms"]):
            merged[row["label"]] = {**row, "runs": runs}
        else:
            kept["runs"] = runs
    return list(merged.values())
