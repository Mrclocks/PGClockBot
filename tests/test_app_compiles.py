"""Every module of the app must compile, and the app must import, so a bad merge cannot ship."""
import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AppCompilesTests(unittest.TestCase):
    def test_every_python_file_compiles(self):
        failures = []
        for path in sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "alembic").rglob("*.py")):
            try:
                compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
            except SyntaxError as exc:
                failures.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
        self.assertEqual(failures, [])

    def test_the_app_and_the_scheduler_import(self):
        for name in ("app.api.app", "app.bot", "app.jobs.scheduler"):
            importlib.import_module(name)


if __name__ == "__main__":
    unittest.main()
