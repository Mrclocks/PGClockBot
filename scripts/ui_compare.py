#!/usr/bin/env python3
"""Compare two panel UI captures from scripts/ui_capture.cjs: pixels and controls.

A cleanup or optimization of the panel must not change how a page looks or what it offers. This
compares two capture directories:

* every screenshot pixel for pixel (both runs mask the same live meters), and
* every view's inventory: link and button text and targets, form actions and field names.

It prints each difference, writes a highlighted image for each changed screenshot to
``<after>/diff/``, and exits 1 when anything differs, 0 when both runs match exactly.

Usage:
    python scripts/ui_compare.py BEFORE_DIR AFTER_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

HIGHLIGHT = (255, 0, 0)


def compare_images(before: Path, after: Path, diff_dir: Path) -> list[str]:
    problems: list[str] = []
    names_before = {p.name for p in (before / "png").glob("*.png")}
    names_after = {p.name for p in (after / "png").glob("*.png")}
    problems += [f"screenshot missing after: {name}" for name in sorted(names_before - names_after)]
    problems += [f"screenshot new after: {name}" for name in sorted(names_after - names_before)]
    for name in sorted(names_before & names_after):
        old_bytes = (before / "png" / name).read_bytes()
        new_bytes = (after / "png" / name).read_bytes()
        if old_bytes == new_bytes:
            continue
        old = Image.open(before / "png" / name).convert("RGB")
        new = Image.open(after / "png" / name).convert("RGB")
        if old.size != new.size:
            problems.append(f"screenshot size changed: {name} {old.size} -> {new.size}")
            continue
        delta = ImageChops.difference(old, new)
        box = delta.getbbox()
        if box is None:
            continue
        changed = sum(1 for pixel in delta.getdata() if pixel != (0, 0, 0))
        problems.append(f"screenshot differs: {name} {changed} px in box {box}")
        diff_dir.mkdir(parents=True, exist_ok=True)
        mask = delta.convert("L").point(lambda value: 255 if value else 0)
        marked = new.copy()
        marked.paste(HIGHLIGHT, mask=mask)
        marked.save(diff_dir / name)
    return problems


def compare_inventory(before: Path, after: Path) -> list[str]:
    old = json.loads((before / "inventory.json").read_text(encoding="utf-8"))
    new = json.loads((after / "inventory.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    for width in sorted(set(old) | set(new)):
        views_old, views_new = old.get(width, {}), new.get(width, {})
        for view in sorted(set(views_old) | set(views_new)):
            a, b = views_old.get(view), views_new.get(view)
            if a == b:
                continue
            if a is None or b is None:
                problems.append(f"{width}px {view}: view {'new' if a is None else 'missing'} after")
                continue
            for key in ("status", "controls", "forms"):
                if a.get(key) != b.get(key):
                    gone = [item for item in a.get(key) or [] if item not in (b.get(key) or [])] if key != "status" else []
                    added = [item for item in b.get(key) or [] if item not in (a.get(key) or [])] if key != "status" else []
                    detail = f"{a.get(key)} -> {b.get(key)}" if key == "status" else f"removed {gone} added {added}"
                    problems.append(f"{width}px {view}: {key} changed: {detail}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    before, after = (Path(arg) for arg in argv)
    problems = compare_inventory(before, after) + compare_images(before, after, after / "diff")
    for problem in problems:
        print(problem)
    shots = len(list((before / "png").glob("*.png")))
    print(f"ui-compare: {shots} screenshots, {len(problems)} differences")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
