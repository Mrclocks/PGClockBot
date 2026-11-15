"""The panel service worker, run in Node: content-keyed static files come from the cache, the rest as before."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.services.pwa import service_worker_js

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PwaServiceWorkerTests(unittest.TestCase):
    def test_node_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sw = Path(tmp) / "sw.js"
            sw.write_text(service_worker_js(), encoding="utf-8")
            result = subprocess.run(
                [shutil.which("node"), str(ROOT / "tests/pwa_service_worker.test.cjs"), str(sw)],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pwa service worker tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
