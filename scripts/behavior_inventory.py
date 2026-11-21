#!/usr/bin/env python3
"""Record every web route and bot handler, and compare two recordings.

A cleanup that only moves code (splitting app.py, admin.py or reply_nav.py) must leave the app
answering the same requests in the same way. This writes what the app exposes to JSON:

* web: method, path, handler, and the names of its dependencies (authentication and permissions);
* bot: every router in dispatch order and, per router and event, each handler with its filters.

Order is kept, because aiogram tries handlers in order and FastAPI matches routes in order. A
handler that moved to another module shows as a changed handler name, which is expected in a
pure move; everything else in the diff is a behaviour change to explain.

Usage:
    python scripts/behavior_inventory.py snapshot before.json
    # ... make the change ...
    python scripts/behavior_inventory.py snapshot after.json
    python scripts/behavior_inventory.py compare before.json after.json [--ignore-modules]

No database or network is used. A placeholder bot token is set only if none is configured.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_TOKEN = "123456:inventory-placeholder-token-000000000"
_ADDRESS = re.compile(r" at 0x[0-9a-f]+")
MAX_DEPTH = 6


def _name(obj: Any) -> str:
    module = getattr(obj, "__module__", "") or ""
    qualname = getattr(obj, "__qualname__", None) or type(obj).__qualname__
    return f"{module}.{qualname}" if module else qualname


def describe(obj: Any, depth: int = 0) -> Any:
    """A stable, address-free description of a filter or a value inside one."""
    if depth > MAX_DEPTH:
        return "..."
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = [describe(item, depth + 1) for item in obj]
        return sorted(items, key=json.dumps) if isinstance(obj, (set, frozenset)) else items
    if isinstance(obj, dict):
        return {str(key): describe(value, depth + 1) for key, value in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    operations = getattr(obj, "_operations", None)
    if isinstance(operations, tuple):
        return {"magic": [describe(op, depth + 1) for op in operations]}
    if callable(obj) and hasattr(obj, "__qualname__"):
        bound_to = getattr(obj, "__self__", None)
        if bound_to is not None and not isinstance(bound_to, type):
            return {"bound": getattr(obj, "__name__", "?"), "of": describe(bound_to, depth + 1)}
        return _name(obj)
    fields = dict(getattr(obj, "__dict__", None) or {})
    for klass in type(obj).__mro__:
        slots = getattr(klass, "__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            if not slot.startswith("__") and hasattr(obj, slot):
                fields.setdefault(slot, getattr(obj, slot))
    if fields:
        public = {key: value for key, value in fields.items() if not key.startswith("__")}
        return {"type": _name(type(obj)), **describe(public, depth + 1)}
    return _ADDRESS.sub("", repr(obj))


def web_routes() -> list[dict[str, Any]]:
    from fastapi.routing import APIRoute

    from app.api.app import create_api_app

    def dependency_names(dependant: Any) -> list[str]:
        names: list[str] = []
        for dep in dependant.dependencies:
            names.append(getattr(dep.call, "__name__", str(dep.call)))
            names.extend(dependency_names(dep))
        return names

    rows = []
    for route in create_api_app().routes:
        entry: dict[str, Any] = {"path": getattr(route, "path", ""), "kind": type(route).__name__}
        if isinstance(route, APIRoute):
            entry.update(
                methods=sorted(route.methods),
                handler=_name(route.endpoint),
                dependencies=sorted(set(dependency_names(route.dependant)) - {"get_db"}),
            )
        rows.append(entry)
    return rows


def _describe_filter(filter_object: Any) -> Any:
    magic = getattr(filter_object, "magic", None)
    return describe(magic if magic is not None else filter_object.callback)


def bot_handlers() -> list[dict[str, Any]]:
    from app.bot import create_dispatcher

    dispatcher = create_dispatcher()
    rows: list[dict[str, Any]] = []

    def walk(router: Any, trail: str) -> None:
        name = trail if router is dispatcher else f"{trail}/{router.name}"
        for event, observer in router.observers.items():
            rows.extend(
                {
                    "router": name,
                    "event": event,
                    "handler": _name(handler.callback),
                    "filters": [_describe_filter(f) for f in handler.filters],
                }
                for handler in observer.handlers
            )
        for child in router.sub_routers:
            walk(child, name)

    walk(dispatcher, "dispatcher")
    return [row for row in rows if not row["handler"].startswith("aiogram.")]


def snapshot(out: Path) -> None:
    os.environ.setdefault("BOT_TOKEN", PLACEHOLDER_TOKEN)
    logging.disable(logging.CRITICAL)
    data = {"web": web_routes(), "bot": bot_handlers()}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{out}: {len(data['web'])} web routes, {len(data['bot'])} bot handlers")


def _strip_modules(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only a handler's function name, so a function moved to another module compares equal."""
    return [{**row, "handler": row.get("handler", "").rsplit(".", 1)[-1]} if "handler" in row else row for row in rows]


def compare(before: Path, after: Path, *, ignore_modules: bool) -> int:
    old = json.loads(before.read_text(encoding="utf-8"))
    new = json.loads(after.read_text(encoding="utf-8"))
    differences = 0
    for section in ("web", "bot"):
        a, b = old[section], new[section]
        if ignore_modules:
            a, b = _strip_modules(a), _strip_modules(b)
        if a == b:
            print(f"{section}: identical ({len(a)})")
            continue
        import difflib

        lines = list(difflib.unified_diff(
            [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in a],
            [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in b],
            fromfile=f"{before}:{section}", tofile=f"{after}:{section}", lineterm="", n=1,
        ))
        differences += sum(1 for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---")))
        print("\n".join(lines))
    print("no behaviour differences" if not differences else f"{differences} changed lines")
    return 1 if differences else 0


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("out", type=Path)
    comp = sub.add_parser("compare")
    comp.add_argument("before", type=Path)
    comp.add_argument("after", type=Path)
    comp.add_argument("--ignore-modules", action="store_true", help="treat a handler moved to another module as unchanged")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        snapshot(args.out)
        return 0
    return compare(args.before, args.after, ignore_modules=args.ignore_modules)


if __name__ == "__main__":
    raise SystemExit(main())
