#!/usr/bin/env python3
"""Turn query-profile JSON lines (web requests, bot taps) into a Markdown report.

    python scripts/query_profile_report.py [--title TITLE] FILE.jsonl [FILE.jsonl ...]

Rows with the same label (route template or button) are merged and the heaviest run is kept, so
``/users/11/edit`` and ``/users/12/edit`` count as one page. The table lists the heaviest first;
below it, every statement shape that ran five times or more in one unit of work (a likely N+1).
When the rows come from runs at more than one data ``scale``, a last table lists every unit whose
statement count grew with the data: the clearest sign of a query per row.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(1, str(Path(__file__).resolve().parent))

from query_counter import REPEAT_THRESHOLD, markdown_table, merge_runs  # noqa: E402 - the scripts directory is put on the path just above


def read_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def repeated_section(rows: list[dict[str, Any]]) -> str:
    lines = ["### Repeated statements (likely N+1)", ""]
    found = sorted(((r["label"], item) for r in rows for item in r.get("repeated", [])),
                   key=lambda pair: pair[1]["count"], reverse=True)
    if not found:
        return "\n".join([*lines, "None.", ""])
    lines += ["| Unit | Times | Statement | Run from |", "|---|---|---|---|"]
    lines += [f"| `{label}` | {item['count']} | `{item['shape']}` | `{item.get('origin', '')}` |"
              for label, item in found]
    return "\n".join(lines) + "\n"


def _grown_shapes(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[int, str]]:
    """The statements that ran more often with more data: (extra runs, where), most first."""
    counts = {(item["shape"], item.get("origin", "")): item["count"] for item in before.get("top", [])}
    grown = []
    for item in after.get("top", []):
        extra = item["count"] - counts.get((item["shape"], item.get("origin", "")), 0)
        if extra > 0:
            grown.append((extra, item.get("origin") or item["shape"][:60]))
    return sorted(grown, reverse=True)


def _steady_runs(rows: list[dict[str, Any]], scale: int) -> dict[str, dict[str, Any]]:
    """The cheapest run of each exact URL (or button) at one scale: caches warm, same tab."""
    steady: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("scale", 0) != scale:
            continue
        key = row.get("url") or row["label"]
        if key not in steady or row["statements"] < steady[key]["statements"]:
            steady[key] = row
    return steady


def growth_section(rows: list[dict[str, Any]]) -> str:
    scales = sorted({row.get("scale", 0) for row in rows})
    if len(scales) < 2:
        return ""
    small, large = scales[0], scales[-1]
    before_runs, after_runs = _steady_runs(rows, small), _steady_runs(rows, large)
    grown = [(key, before, after_runs[key], _grown_shapes(before, after_runs[key]))
             for key, before in before_runs.items()
             if key in after_runs and after_runs[key]["statements"] > before["statements"]]
    # A statement run many more times is work per row. A unit with only a few extra statements
    # read a cache that had expired since the other run (settings, badges), not more rows.
    per_row = [item for item in grown if item[3] and item[3][0][0] >= REPEAT_THRESHOLD]
    per_row.sort(key=lambda item: item[2]["statements"] - item[1]["statements"], reverse=True)
    lines = [f"### Statements that grow with the data (scale {small} -> {large})", "",
             "Each URL or button at its cheapest run in both, so a cold cache or another tab does not count.", ""]
    if per_row:
        lines += ["| Unit | Statements, small | Statements, large | Extra | Grown statements |", "|---|---|---|---|---|"]
        lines += [f"| `{key}` | {before['statements']} | {after['statements']} | "
                  f"+{after['statements'] - before['statements']} | "
                  + "<br>".join(f"+{extra}× `{where}`" for extra, where in shapes[:3]) + " |"
                  for key, before, after, shapes in per_row]
    else:
        lines.append("None: no statement runs once per row.")
    others = len(grown) - len(per_row)
    if others:
        lines += ["", f"{others} more units differ by fewer than {REPEAT_THRESHOLD} runs of any one statement: "
                      "a cache that expired between the two runs, not work per row."]
    return "\n".join(lines) + "\n"


def report(rows: list[dict[str, Any]], *, title: str, limit: int) -> str:
    merged = merge_runs(rows)
    total = sum(r["statements"] for r in rows)
    summary = (f"{len(rows)} units of work, {len(merged)} distinct, {total} statements in all; "
               f"{sum(1 for r in merged if r.get('repeated'))} distinct units repeat a statement 5+ times.\n")
    sections = [summary, markdown_table(merged, title=title, limit=limit), repeated_section(merged), growth_section(rows)]
    return "\n".join(section for section in sections if section)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--title", default="Heaviest units of work")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    print(report(read_rows(args.files), title=args.title, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
