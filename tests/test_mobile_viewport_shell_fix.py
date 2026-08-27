"""Mobile shell — document scroll, stable svh min-height, no nested main scroller."""

from __future__ import annotations

import unittest
from pathlib import Path

from css_blocks import at_rule, has_rule, rule


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
BASE = ROOT / "app/web/templates/base.html"
JS = ROOT / "app/web/static/panel.js"
PWA = ROOT / "app/services/pwa.py"


class MobileViewportShellFixTests(unittest.TestCase):
    def _mobile(self) -> str:
        return at_rule(CSS.read_text(encoding="utf-8"), "@media (max-width: 900px)")

    def _css(self) -> str:
        return CSS.read_text(encoding="utf-8")

    def test_mobile_shell_flex_fill_not_viewport_units(self):
        mobile = self._mobile()
        shell = mobile.split(".shell {", 1)[1].split("  .topbar", 1)[0]
        side = mobile.split("  .side {", 1)[1].split("  .side.open", 1)[0]
        self.assertIn("height: auto;", shell)
        self.assertIn("flex: 1 0 auto;", shell)
        self.assertIn("padding-bottom: 0;", side)
        self.assertNotIn("transform: translateX", side)
        self.assertIn("right: calc(-1 * min(300px, 86vw) - 24px);", side)
        foot = mobile.split(".side .side-foot", 1)[1][:320]
        self.assertIn("padding-bottom: var(--bottom-inset);", foot)
        self.assertIn("max-height: calc(var(--footer-bar-h) + var(--bottom-inset));", foot)
        self.assertNotIn("var(--foot-gap) + var(--safe-bottom)", side)
        closed = mobile.split(".side:not(.open)", 1)[1][:160]
        self.assertNotIn("height: 0;", closed)
        shell_block = shell.split("}", 1)[0]
        for unit in ("100dvh", "100lvh", "100vh", "var(--vvh"):
            self.assertNotIn(f"height: {unit}", shell_block)
            self.assertNotIn(f"height:{unit}", shell_block)
        self.assertIn("padding-bottom: 0;", shell)
        self.assertIn("min-height: 100svh;", shell_block)

    def test_document_is_scroll_owner(self):
        mobile = self._mobile()
        html = mobile.split("html:has(.shell) {\n", 1)[1].split("}", 1)[0]
        body = mobile.split("html:has(.shell) body {\n", 1)[1].split("}", 1)[0]
        main = mobile.split("  .main {", 1)[1].split("  .main-body", 1)[0]
        self.assertIn("min-height: 100svh;", html)
        self.assertIn("height: auto;", html)
        self.assertNotRegex(html, r"(?m)^\s*height:\s*100%;")
        self.assertIn("overflow-x: visible;", html)
        self.assertIn("overflow-y: visible;", html)
        self.assertNotIn("overflow-x: hidden;", html)
        self.assertIn("min-height: 100svh;", body)
        self.assertIn("overflow-x: visible;", body)
        self.assertIn("overflow-y: visible;", body)
        self.assertNotIn("overflow-y: auto;", body)
        self.assertIn("overflow-x: clip;", main)
        self.assertIn("overflow-y: visible;", main)
        self.assertNotIn("overflow-y: auto;", main)
        self.assertIn("padding: var(--page-title-gap) var(--space-2) 0;", main)
        self.assertIn("background: var(--background);", main)

    def test_sidebar_fixed_with_pointer_events_guard(self):
        mobile = self._mobile()
        side = mobile.split("  .side {", 1)[1].split("  .side.open", 1)[0]
        self.assertIn("position: fixed;", side)
        self.assertIn("bottom: 0;", side)
        self.assertIn("pointer-events: none;", mobile.split(".side:not(.open)", 1)[1][:120])

    def test_bottom_inset_token_and_shared_footers(self):
        css = self._css()
        self.assertIn("--bottom-inset:", css[:5000])
        self.assertIn("--footer-bar-h:", css[:5000])
        mobile = self._mobile()
        site = mobile.split("  .site-footer {", 1)[1].split("  .footer-meta", 1)[0]
        side_foot = mobile.split(".side .side-foot", 1)[1][:360]
        self.assertIn("padding-bottom: var(--bottom-inset);", site)
        self.assertIn("padding-bottom: var(--bottom-inset);", side_foot)
        shared = "min-height: calc(var(--footer-bar-h) + var(--bottom-inset));"
        self.assertIn(shared, site)
        self.assertIn(shared, side_foot)
        shared_max = "max-height: calc(var(--footer-bar-h) + var(--bottom-inset));"
        self.assertIn(shared_max, site)
        self.assertIn(shared_max, side_foot)

    def test_dark_color_scheme_and_no_vvh(self):
        base = BASE.read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        css = self._css()
        self.assertIn("color-scheme: dark;", css[:200])
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn("color-scheme: dark;", css.split('html[data-theme="dark"]', 1)[1][:80])
        self.assertIn('html[data-theme="light"]', css)
        self.assertIn("color-scheme: light;", css.split('html[data-theme="light"]', 1)[1][:80])
        self.assertIn("meta-color-scheme", base)
        self.assertIn('scheme.setAttribute("content"', base)
        self.assertIn("metaScheme", js)
        self.assertNotIn("leave theme-color transparent", js)
        self.assertNotIn("--vvh", base)
        self.assertNotIn("--vvh", css)
        self.assertNotIn("visualViewport", base)
        self.assertNotIn("visualViewport", js)
        self.assertNotIn("__pgPinSafariOverlay", js)
        self.assertNotIn("iOS drawer glass", css)
        self.assertNotIn("html.ios .side.open", css)

    def test_sw_fallback_ignores_query_for_panel_assets(self):
        sw = PWA.read_text(encoding="utf-8")
        self.assertIn("pgclock-shell-v31", sw)
        self.assertIn("matchIgnoreSearch", sw)
        self.assertIn("isVersionedPanelAsset", sw)

    def test_instant_close_kept_no_glass(self):
        mobile = self._mobile()
        css = self._css()
        self.assertIn("side-nav-closing", mobile)
        self.assertNotIn("html.ios-safari .shell", mobile)
        self.assertNotIn("html.ios .side.open", css)
        js = JS.read_text(encoding="utf-8")
        self.assertIn("side-nav-closing", js)
        self.assertIn("setOpen(false, true)", js)
        # One backdrop geometry rule, full-bleed, defined once outside the
        # media query — the drawer and the dim must share one bottom edge.
        self.assertFalse(has_rule(mobile, ".side-backdrop"))
        self.assertFalse(has_rule(mobile, ".side-backdrop, .side-backdrop.show"))
        back = rule(css, ".side-backdrop")
        self.assertIn("inset: 0;", back)
        self.assertIn("position: fixed;", back)


if __name__ == "__main__":
    unittest.main()
