"""Every import inside a function in ``app/`` names something that exists.

A function-local import only runs when its function does, so a name that was moved or renamed elsewhere breaks
just that screen at the moment someone opens it (the shop's «📢 پیام گروهی» answered «خطایی رخ داد» this way).
This imports each target module and checks each imported name, so the move is caught in CI instead.
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _local_imports() -> list[tuple[str, int, str, str]]:
    """(file, line, module, name) for every ``from app… import name`` inside a function."""
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("app"):
                    rel = str(path.relative_to(APP.parent))
                    found.extend((rel, node.lineno, node.module, alias.name) for alias in node.names)
    return found


def _resolves(module_name: str, name: str) -> bool:
    module = importlib.import_module(module_name)
    if hasattr(module, name):
        return True
    try:
        importlib.import_module(f"{module_name}.{name}")
    except ImportError:
        return False
    return True


class LocalImportTests(unittest.TestCase):
    def test_every_function_local_import_resolves(self) -> None:
        imports = _local_imports()
        self.assertTrue(imports)
        missing = sorted({f"{f}:{line} {mod}.{name}" for f, line, mod, name in imports if not _resolves(mod, name)})
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
