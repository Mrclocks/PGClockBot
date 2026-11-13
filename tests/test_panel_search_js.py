"""Browser rules of the sidebar search: result rendering and escaping, and the Ctrl/⌘+K and "/" hotkey."""

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


if __name__ == "__main__":
    unittest.main()
