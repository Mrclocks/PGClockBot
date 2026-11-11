"""Permission accordion counters stay visible and sync from checkbox state."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel-ds.css"
JS = ROOT / "app/web/static/panel-ds.js"


class DsPermCounterTests(unittest.TestCase):
    def test_group_counter_layout_does_not_clip_badge(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        title = css.split(".ds .ds-acc-title {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 0", title)
        self.assertIn("text-overflow: ellipsis", title)
        meta = css.split(".ds .ds-meta {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 0 0 auto", meta)

    def test_fragment_dom_ready_re_syncs_panel_ds(self) -> None:
        js = JS.read_text(encoding="utf-8")
        self.assertIn('document.addEventListener("panel:dom-ready"', js)
        self.assertIn("syncAll(root)", js)

    def test_sync_perms_fills_group_counts(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "tests" / "ds_perm_counters.test.cjs")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
