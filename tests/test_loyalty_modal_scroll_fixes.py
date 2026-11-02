"""Loyalty top-referrers column width + modal scroll lock."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
JS = ROOT / "app/web/static/panel.js"
LOYALTY = ROOT / "app/web/templates/loyalty.html"


class LoyaltyTopReferrersColumns(unittest.TestCase):
    def test_invite_count_uses_col_count(self):
        html = LOYALTY.read_text(encoding="utf-8")
        self.assertIn('class="col-count"', html)
        self.assertIn('class="mono col-count"', html)
        self.assertIn("برترین معرف‌ها", html)
        self.assertIn("loyalty-referrers", html)

    def test_col_count_css_shrinks_to_content(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".col-count {", css)
        block = css.split(".col-count {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 50%;", block)
        # 50% first-column is loyalty-only — never all compact tables
        self.assertNotIn(
            ".table-compact th:not(.col-count):first-child,\n.table-compact td:not(.col-count):first-child {",
            css,
        )
        self.assertIn(".loyalty-referrers .table-compact th:not(.col-count):first-child", css)


class ModalScrollLockTests(unittest.TestCase):
    def test_locks_main_scroll_containers(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("body.modal-open .main,", css)
        self.assertIn("body.modal-open .side {", css)
        self.assertIn("overflow: hidden !important;", css)
        self.assertIn("overscroll-behavior: contain;", css)

    def test_settings_modal_panel_scrolls_like_others(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".settings-modal-panel {", css)
        block = css.split(".settings-modal-panel {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: hidden;", block)
        self.assertIn("overscroll-behavior: contain;", block)
        self.assertNotIn("overflow-y: auto;", block)
        body = css.split(".settings-modal-body {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: visible;", body)

    def test_edit_modal_body_is_not_nested_scroller(self):
        """User/reseller edit fragments must not steal scroll from .ui-modal-scroll."""
        css = CSS.read_text(encoding="utf-8")
        body = css.split(".edit-modal-body {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 0 0 auto;", body)
        self.assertIn("overflow: visible;", body)
        self.assertNotIn("overflow-x: hidden;", body)
        self.assertNotIn("overflow-y: auto;", body)
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn('id="user-edit-body"', users)
        self.assertIn("edit-modal-body", users)
        resellers = (ROOT / "app/web/templates/resellers.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("edit-modal-body", resellers)

    def test_js_lock_helpers_exist(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function lockPageScroll", js)
        self.assertIn("function unlockPageScroll", js)
        self.assertIn("installModalScrollGuards", js)
        # <html> is never flagged/locked — that froze iOS Safari's toolbar.
        self.assertNotIn("documentElement.classList.add('modal-open')", js)
        self.assertIn("document.body.classList.add('modal-open')", js)
        self.assertIn("Always-on guards", js)
        self.assertIn("Prefer the inner scroll shell", js)
        self.assertIn("isModalInteriorScroller", js)

    def test_modal_radius_clip_on_panel_opacity_motion(self):
        """Panel owns opaque chrome + radius clip; open motion is opacity-only."""
        css = CSS.read_text(encoding="utf-8")
        self.assertIn('.ui-modal-panel[data-scroll-shell="1"]', css)
        shell = css.split('.ui-modal-panel[data-scroll-shell="1"] {', 1)[1].split("}", 1)[0]
        self.assertIn("overflow: hidden;", shell)
        self.assertIn("background: var(--bg-card);", shell)
        self.assertNotIn("overflow: visible;", shell)
        self.assertNotIn("background: transparent;", shell)
        # Open/close must not transform the clipping box (WebKit corner sliver).
        in_kf = css.split("@keyframes ui-modal-in {", 1)[1].split("}", 1)[0]
        out_kf = css.split("@keyframes ui-modal-out {", 1)[1].split("}", 1)[0]
        self.assertIn("opacity:", in_kf)
        self.assertIn("opacity:", out_kf)
        self.assertNotIn("transform:", in_kf)
        self.assertNotIn("transform:", out_kf)
        # Shared shell scroll rule (ui-modal + settings-modal)
        shell_scroll_sel = (
            '.ui-modal-panel[data-scroll-shell="1"] > .ui-modal-scroll,\n'
            '.settings-modal-panel[data-scroll-shell="1"] > .ui-modal-scroll {'
        )
        self.assertIn(shell_scroll_sel, css)
        scroll_chrome = css.split(shell_scroll_sel, 1)[1].split("}", 1)[0]
        self.assertIn("background: transparent;", scroll_chrome)
        # Equal vertical air: flex column + gap + symmetric padding
        self.assertIn("display: flex;", scroll_chrome)
        self.assertIn("flex-direction: column;", scroll_chrome)
        self.assertIn("gap: var(--space-3);", scroll_chrome)
        self.assertIn("padding: calc(var(--space-3) + var(--modal-scrollbar-room, 8px));", scroll_chrome)
        # Both edges: RTL gutter mismatch + overlay thumbs on the physical right
        self.assertIn("scrollbar-gutter: stable both-edges;", scroll_chrome)
        base_sel = ".ui-modal-scroll {\n"
        self.assertIn(base_sel, css)
        base_scroll = css.split(base_sel, 1)[1].split("}", 1)[0]
        self.assertIn("scrollbar-gutter: stable both-edges;", base_scroll)
        self.assertIn(".ui-modal-scroll::-webkit-scrollbar {", css)
        # Global room stays 8px — #586's 14px narrowed every modal
        self.assertIn("--modal-scrollbar-room: 8px;", css)
        self.assertNotIn("--modal-scrollbar-room: 14px;", css)
        # Body must match shared shell padding (no extra 14px inset)
        self.assertNotIn(
            "#modal-user-edit .ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-scroll {",
            css,
        )
        self.assertNotIn("padding-inline: calc(var(--space-3) + 14px);", css)
        # Shared frost tokens + sticky frosted head/footer
        self.assertIn("--modal-frost-bg:", css)
        self.assertIn("color-mix(in srgb, var(--bg-card) 52%, transparent)", css)
        self.assertIn("--modal-save-pad-y: var(--space-2_5);", css)
        shared_sel = (
            ".ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-scroll > .ui-modal-head,\n"
            ".ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-sticky-save {"
        )
        self.assertIn(shared_sel, css)
        shared_block = css.split(shared_sel, 1)[1].split("}", 1)[0]
        self.assertIn("background: var(--modal-frost-bg);", shared_block)
        self.assertIn("backdrop-filter: var(--modal-frost-blur);", shared_block)
        head_sel = (
            '.ui-modal-panel[data-scroll-shell="1"] > .ui-modal-scroll > .ui-modal-head {'
        )
        self.assertIn(head_sel, css)
        head_block = css.split(head_sel, 1)[1].split("}", 1)[0]
        self.assertIn("position: sticky;", head_block)
        self.assertIn("padding-bottom: var(--space-2);", head_block)
        # Overlay footer (absolute) so content frosts under it like the head
        save_anchor = (
            ".ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-sticky-save {\n"
            "  position: absolute;"
        )
        self.assertIn(save_anchor, css)
        save_block = css.split(save_anchor, 1)[1].split("}", 1)[0]
        self.assertIn("display: flex;", save_block)
        self.assertIn("align-items: stretch;", save_block)
        self.assertIn("padding-block: var(--modal-save-pad-y);", save_block)
        self.assertIn("justify-content: center;", save_block)
        self.assertIn(
            ".ui-modal-panel[data-scroll-shell=\"1\"]:has(> .ui-modal-sticky-save)"
            " > .ui-modal-scroll {",
            css,
        )
        btn_sel = (
            ".ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-sticky-save > .btn,\n"
            ".ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-sticky-save > a.btn,\n"
            ".ui-modal-panel[data-scroll-shell=\"1\"] > .ui-modal-sticky-save > button {"
        )
        self.assertIn(btn_sel, css)
        btn_block = css.split(btn_sel, 1)[1].split("}", 1)[0]
        self.assertIn("width: 100%;", btn_block)
        domain = (
            ROOT / "app/web/templates/_domain_settings_form.html"
        ).read_text(encoding="utf-8")
        pending = (
            ROOT / "app/web/templates/_settings_pending_orders.html"
        ).read_text(encoding="utf-8")
        self.assertIn("ui-modal-sticky-save", domain)
        self.assertNotIn("ui-modal-sticky-save", pending)
        js = JS.read_text(encoding="utf-8")
        self.assertIn('panel.dataset.scrollShell = \'1\'', js)
        self.assertIn("corner clip", js)
        self.assertIn("function pinModalStickySave", js)
        self.assertIn("data-save-home", js)
        shell_fn = js.split("ensureModalScrollShell")[1].split("function canScrollInside")[0]
        self.assertIn("while (panel.firstChild)", shell_fn)
        self.assertIn("pinModalStickySave", shell_fn)
        self.assertNotIn("keepOutsideScroll", shell_fn)
        self.assertNotIn("userEditOnly", shell_fn)
        self.assertIn("syncPaySettingsCards", js)
        self.assertIn("data-pay-settings-for", js)


class ModalLayoutPolishTests(unittest.TestCase):
    def test_modal_title_macro_and_css(self):
        macros = (ROOT / "app/web/templates/macros.html").read_text(encoding="utf-8")
        self.assertIn("{% macro modal_title(", macros)
        self.assertIn('class="modal-title"', macros)
        self.assertIn("modal-title-caption", macros)
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".modal-title {", css)
        self.assertIn(".modal-title-caption {", css)

    def test_desktop_modal_grids_cap_at_two_columns(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("@media (min-width: 641px)", css)
        self.assertIn(
            ".ui-modal-panel .form-row,\n  .ui-modal-panel .pay-dest-fields {",
            css,
        )
        # Two-col lock (never auto-fit into a third track inside modals)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr));",
            css,
        )

    def test_modal_color_chips_full_width(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(
            ".ui-modal-panel .pay-dest-color .plan-color-card {",
            css,
        )
        self.assertIn(
            ".ui-modal-panel .support-fields .item-color-field .plan-color-card {",
            css,
        )
        field = (ROOT / "app/web/templates/_settings_field.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("referral_bonus", field)
        self.assertIn("btn_pay_card", field)

    def test_plan_modal_uses_modal_title(self):
        plans = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("modal_title('plans'", plans)
        self.assertIn("heading_id='plan-unified-title'", plans)
        # Name/price/duration/volume share one form-row (2-col on desktop via CSS)
        self.assertIn("نام پلن", plans)
        block = plans.split('id="panel-user-fixed"', 1)[1].split("</form>", 1)[0]
        self.assertIn('class="form-row"', block)
        self.assertIn('name="price"', block)
        self.assertIn('name="duration_days"', block)
        self.assertIn('name="data_limit_gb"', block)


if __name__ == "__main__":
    unittest.main()
