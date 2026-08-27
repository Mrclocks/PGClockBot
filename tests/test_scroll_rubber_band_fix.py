"""Scroll model — mobile uses document scroll, desktop keeps inner main scroll."""

from __future__ import annotations

import unittest
from pathlib import Path

from css_blocks import at_rule, rule


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
JS = ROOT / "app/web/static/panel.js"

MOBILE_MQ = "@media (max-width: 900px)"
DESKTOP_MQ = "@media (min-width: 901px)"


class PageScrollContainerTests(unittest.TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")
        self.mobile = at_rule(self.css, MOBILE_MQ)
        self.desktop = at_rule(self.css, DESKTOP_MQ)

    def test_desktop_main_allows_vertical_overscroll_bounce(self):
        self.assertIn("overscroll-behavior-y: auto;", rule(self.css, ".main"))
        self.assertIn("overscroll-behavior-y: auto;", rule(self.css, ".side"))

    def test_mobile_document_is_scroll_owner_not_main(self):
        shell = rule(self.mobile, ".shell")
        main = rule(self.mobile, ".main")
        html = rule(self.mobile, "html:has(.shell)")
        body = rule(self.mobile, "html:has(.shell) body")
        for block in (html, body, shell):
            self.assertIn("overflow-x: visible;", block)
            self.assertIn("overflow-y: visible;", block)
            self.assertNotIn("overflow-x: hidden;", block)
            self.assertNotIn("overflow-y: auto;", block)
            self.assertNotIn("overflow: hidden;", block)
            self.assertIn("min-height: 100svh;", block)
        self.assertIn("height: auto;", html)
        self.assertIn("height: auto;", shell)
        self.assertIn("flex: 1 0 auto;", main)
        self.assertIn("flex: 1 0 auto;", shell)
        # Fill height is svh only — dvh/lvh would reflow the document while the
        # browser chrome animates.
        for unit in ("100dvh", "100lvh", "100vh"):
            self.assertNotIn(unit, shell)
        self.assertIn("overflow-x: clip;", main)
        self.assertIn("overflow-y: visible;", main)
        self.assertNotIn("overflow-y: auto;", main)

    def test_open_overlays_never_lock_the_root_scroller(self):
        """The root scroller must stay scrollable while any overlay is open.

        Blocking it (overflow:hidden / touch-action:none / overscroll:none on
        html or body) makes iOS Safari snap its bottom toolbar to the expanded
        state and stop painting the strip the toolbar covered — the solid bar
        that survived the overlay close, because Safari only retracts again on a
        real page scroll. Background touches are contained by the overlays.
        """
        css = self.css
        self.assertNotIn("html:has(body.nav-open)", css)
        self.assertNotIn("html.modal-open", css)
        for selector in (
            "body.nav-open {",
            "body.modal-open {",
            "html.nav-open",
            "body.nav-open,",
            "body.modal-open,",
        ):
            self.assertNotIn(selector, css, selector)

        # The drawer backdrop owns containment and is full-bleed, so no
        # background pixel is left pannable.
        back = rule(css, ".side-backdrop")
        self.assertIn("position: fixed;", back)
        self.assertIn("inset: 0;", back)
        self.assertIn("touch-action: none;", back)
        self.assertIn("overscroll-behavior: contain;", back)
        # The topbar paints above the backdrop, so it contains touches itself.
        self.assertIn(
            "touch-action: none;", rule(self.mobile, "body.nav-open .topbar")
        )
        # Modal backdrop does the same job for modals.
        self.assertIn("touch-action: none;", rule(css, ".ui-modal-backdrop"))

        # Freezing .main / .side is only correct on desktop, where they are the
        # scrollers and the root never scrolls.
        frozen = rule(self.desktop, "body.modal-open .main, body.modal-open .side")
        self.assertIn("overflow: hidden !important;", frozen)
        self.assertNotIn("body.modal-open", self.mobile)
        self.assertNotIn("body.nav-open .main", self.mobile)

    def test_modal_scroll_lock_never_touches_the_document(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function lockPageScroll", js)
        self.assertNotIn("documentElement.classList.add('modal-open')", js)
        self.assertNotIn("documentElement.classList.remove('modal-open')", js)
        self.assertNotIn("documentElement.classList.toggle('modal-open'", js)
        # No document scroll save/restore: the document scroll is never frozen.
        self.assertNotIn("modalScrollY", js)


class ModalOverscrollTests(unittest.TestCase):
    def test_modal_root_uses_contain_not_none(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("overscroll-behavior: contain;", rule(css, ".ui-modal"))

    def test_modal_guards_skip_interior_edge_prevent_default(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function isModalInteriorScroller", js)


if __name__ == "__main__":
    unittest.main()
