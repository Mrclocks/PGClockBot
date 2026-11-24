"""Print the Persian release notes of one version as GitHub release markdown."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

NOTES_FILE = Path(__file__).resolve().parent.parent / "app" / "services" / "release_notes.py"
NOTES_VARIABLE = "RELEASE_NOTES_FA"


def load_notes(path: Path) -> dict[str, list[str]]:
    """Read the notes dict without importing the app and its dependencies."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if target is None and isinstance(node, ast.Assign):
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == NOTES_VARIABLE:
            return ast.literal_eval(node.value)
    raise ValueError(f"{NOTES_VARIABLE} not found in {path}")


def render(version: str, notes: dict[str, list[str]]) -> str:
    lines = notes.get(version)
    if not lines:
        return f"PGClockBot {version}"
    return "\n".join(f"- {line}" for line in lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes_for_tag.py <version>", file=sys.stderr)
        return 2
    print(render(argv[1].removeprefix("v"), load_notes(NOTES_FILE)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
