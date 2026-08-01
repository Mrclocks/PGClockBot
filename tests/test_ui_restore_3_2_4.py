"""Panel appearance/spacing restored to 3.0.4 (release 3.2.4)."""

from __future__ import annotations

import hashlib
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
UI_PATHS = [
    "app/web/static/panel.css",
    "app/web/static/panel.js",
    "app/web/templates/_settings_update.html",
    "app/web/templates/plans.html",
    "app/web/templates/base.html",
]


class UiRestore304Tests(unittest.TestCase):
    def test_css_has_no_post_304_spacing_tokens(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("--page-title-gap", css)
        self.assertNotIn("--space-5", css)
        self.assertNotIn("--space-6", css)
        self.assertIn("--space-1: 8px", css)
        self.assertIn("--space-2: 12px", css)
        self.assertIn("--section-gap: 16px", css)

    def test_core_ui_files_match_304_commit(self):
        """Byte-identical to commit 8f4567c (3.0.4) for restored UI surfaces."""
        for rel in UI_PATHS:
            with self.subTest(rel=rel):
                current = (ROOT / rel).read_bytes()
                old = subprocess.check_output(
                    ["git", "show", f"8f4567c:{rel}"],
                    cwd=ROOT,
                )
                self.assertEqual(
                    hashlib.sha256(current).hexdigest(),
                    hashlib.sha256(old).hexdigest(),
                    msg=f"{rel} differs from 3.0.4 (8f4567c)",
                )


if __name__ == "__main__":
    unittest.main()
