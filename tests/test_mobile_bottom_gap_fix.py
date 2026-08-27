"""Mobile bottom-gap + iOS scroll-viewport stability tests."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
IOS_PROBE = ROOT / "tests/mobile_ios_scroll_probe.py"
LAYOUT_PROBE = ROOT / "tests/mobile_layout_probe.py"


class MobileBottomGapCssTests(unittest.TestCase):
    def _mobile(self) -> str:
        return CSS.read_text(encoding="utf-8").split("@media (max-width: 900px)", 1)[1]

    def test_document_scroll_not_nested_main(self):
        mobile = self._mobile()
        main = mobile.split("  .main {", 1)[1].split("  .main-body", 1)[0]
        shell = mobile.split(".shell {", 1)[1].split("  .topbar", 1)[0]
        footer = mobile.split("  .site-footer {", 1)[1].split("  .footer-meta", 1)[0]
        body = mobile.split("html:has(.shell) body {\n", 1)[1].split("}", 1)[0]
        html = mobile.split("html:has(.shell) {\n", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-x: clip;", main)
        self.assertIn("overflow-y: visible;", main)
        self.assertNotIn("overflow-y: auto;", main)
        self.assertIn("flex: 1 0 auto;", main)
        self.assertIn("flex: 1 0 auto;", shell)
        self.assertIn("height: auto;", shell)
        self.assertIn("min-height: 100svh;", html)
        # -webkit-fill-available computes to `stretch` in Chromium and silently
        # collapses the fill chain; 100% + 100svh is the whole ladder now.
        self.assertNotIn("-webkit-fill-available", mobile)
        self.assertIn("min-height: 100%;", html)
        self.assertIn("height: auto;", html)
        self.assertIn("overflow-x: visible;", html)
        self.assertIn("overflow-y: visible;", html)
        self.assertNotIn("overflow-x: hidden;", html)
        self.assertIn("min-height: 100svh;", body)
        self.assertIn("min-height: 100%;", body)
        self.assertIn("overflow-x: visible;", body)
        self.assertIn("overflow-y: visible;", body)
        self.assertNotIn("overflow-y: auto;", body)
        self.assertIn("display: flex;", body)
        self.assertIn("margin-top: auto;", footer)
        self.assertIn("overflow-x: visible;", shell)
        self.assertIn("overflow-x: clip;", main)
        shell_block = shell.split("}", 1)[0]
        for unit in ("100dvh", "100lvh", "100vh"):
            self.assertNotIn(unit, shell_block)

    def test_shell_owns_safe_bottom_once(self):
        mobile = self._mobile()
        shell = mobile.split(".shell {", 1)[1].split("  .topbar", 1)[0]
        main = mobile.split("  .main {", 1)[1].split("  .main-body", 1)[0]
        side = mobile.split("  .side {", 1)[1].split("  .side.open", 1)[0]
        foot = mobile.split("  .site-footer {", 1)[1].split("  .footer-meta", 1)[0]
        self.assertIn("padding-bottom: 0;", shell)
        # Bottom inset is owned by both footers — NOT by .main (that split separators).
        self.assertIn("padding: var(--page-title-gap) var(--space-2) 0;", main)
        self.assertIn("padding-bottom: var(--bottom-inset);", foot)
        self.assertIn("padding-bottom: var(--bottom-inset);", mobile.split(".side .side-foot", 1)[1][:200])
        self.assertIn("padding-bottom: 0;", side)
        self.assertIn("min-height: 100svh;", shell)

    def test_no_viewport_js_hacks(self):
        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        mobile = self._mobile()
        self.assertNotIn("--vvh", base)
        self.assertNotIn("visualViewport", base)
        self.assertNotIn("visualViewport", js)
        self.assertNotIn("--vvh", js)
        self.assertNotIn("__pgPinSafariOverlay", js)
        self.assertNotIn("--safari-overlay", mobile)


@unittest.skipUnless(IOS_PROBE.exists(), "ios scroll probe missing")
class MobileIosScrollStabilityTests(unittest.TestCase):
    def test_first_scroll_does_not_change_footer_gap(self):
        proc = subprocess.run(
            [sys.executable, str(IOS_PROBE)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if proc.returncode != 0:
            self.fail(proc.stdout + "\n" + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertFalse(data["nested_main_scroller"])
        analysis = data["analysis"]
        self.assertLessEqual(analysis["shell_bottom_delta_px"], 1)
        self.assertLessEqual(analysis["footer_bottom_delta_px"], 1)
        if analysis["gap_delta"] is not None:
            self.assertLessEqual(abs(analysis["gap_delta"]), 2)


@unittest.skipUnless(LAYOUT_PROBE.exists(), "layout probe missing")
class MobileLayoutGeometryTests(unittest.TestCase):
    def _run(self, fixture: str) -> dict:
        env = {**dict(__import__("os").environ), "MOBILE_PROBE_FIXTURE": fixture}
        proc = subprocess.run(
            [sys.executable, str(LAYOUT_PROBE)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
        )
        if proc.returncode != 0:
            self.fail(proc.stdout + "\n" + proc.stderr)
        return json.loads(proc.stdout)

    def test_short_page_shell_geometry(self):
        out = self._run("tests/fixtures/mobile_shell_probe.html")
        self.assertTrue(out["checks"]["footer_gap_is_foot_gap_only"])
        self.assertTrue(out["checks"]["no_shell_viewport_gap"])


if __name__ == "__main__":
    unittest.main()
