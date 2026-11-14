"""Browser rules of the panel search (rendering, escaping, Ctrl/⌘+K and "/") and of the phone keyboard check."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PanelSearchBrowserTests(unittest.TestCase):
    def test_node_rules(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests/panel_search.test.cjs")],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("panel_search tests passed", result.stdout)

    def test_keyboard_rules(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests/panel_keyboard.test.cjs")],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("panel_keyboard tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
