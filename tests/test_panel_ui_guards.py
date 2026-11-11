"""Guardrails against UI regressions that keep coming back."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
JS = ROOT / "app/web/static/panel.js"
LOGIN = ROOT / "app/web/templates/login.html"
SECURITY = ROOT / "app/web/templates/security.html"


class NoZoomCssGuardTests(unittest.TestCase):
    def test_control_font_size_token_is_16px(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertRegex(css, r"--control-fs:\s*16px")

    def test_inputs_do_not_use_title_size(self):
        css = CSS.read_text(encoding="utf-8")
        # The global form-control block must not shrink below 16px via --title-size.
        bad = re.findall(
            r"(?ms)^(input(?:,\s*select,\s*textarea)?\s*\{[^}]*font-size:\s*var\(--title-size\))",
            css,
        )
        self.assertEqual(bad, [], msg="input/select/textarea must not use --title-size (causes iOS zoom)")

    def test_control_fs_enforced_with_important(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("font-size: var(--control-fs) !important", css)

    def test_js_has_no_zoom_enforcer(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("Permanent no-zoom", js)
        self.assertIn("fontSize", js)


class LoginUsernameGuardTests(unittest.TestCase):
    def test_login_username_not_hardcoded_admin(self):
        src = LOGIN.read_text(encoding="utf-8")
        self.assertNotRegex(src, r'name="username"[^>]*value="admin"')
        self.assertNotRegex(src, r"value=\{\{\s*username\s+or\s+['\"]admin['\"]")
        self.assertIn('value="{{ username or \'\' }}"', src)

    def test_security_username_fields_not_prefilled(self):
        src = SECURITY.read_text(encoding="utf-8")
        self.assertIn('name="old_username"', src)
        self.assertIn('name="new_username"', src)
        self.assertNotIn('value="{{ current_username }}"', src)
        self.assertNotIn('placeholder="{{ current_username }}"', src)


class PwToggleCssTests(unittest.TestCase):
    def test_pw_toggle_uses_margin_auto_centering(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".pw-field input", css)
        self.assertIn("margin-top: 0", css)
        block = re.search(r"(?ms)\.pw-toggle\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("margin-block: auto", body)
        self.assertIn("top: 0", body)
        self.assertIn("bottom: 0", body)


class FieldHintAndPlaceholderTests(unittest.TestCase):
    def test_placeholder_is_rtl_right_aligned(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("input::placeholder", css)
        block = re.search(r"(?ms)input::placeholder,\s*textarea::placeholder\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        # All placeholders: right-aligned + theme muted (not hardcoded grey)
        self.assertIn("text-align: right", body)
        self.assertIn("direction: rtl", body)
        self.assertIn("color: var(--placeholder)", body)
        self.assertIn("--placeholder:", css)
        self.assertIn('html[data-theme="light"]', css)
        # LTR fields still show placeholder on the right
        self.assertIn('input[dir="ltr"]::placeholder', css)
        ltr = re.search(
            r"(?ms)input\[dir=\"ltr\"\]::placeholder,\s*textarea\[dir=\"ltr\"\]::placeholder\s*\{([^}]+)\}",
            css,
        )
        self.assertIsNotNone(ltr)
        self.assertIn("text-align: right", ltr.group(1))
        self.assertIn("color: var(--placeholder)", ltr.group(1))

    def test_field_help_ordered_below_control(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("order: 10", css)
        self.assertIn(".form-field > small.muted", css)
        # Hints under controls need clear gap + theme muted color (not hardcoded grey)
        self.assertIn("margin-top: var(--space-1)", css)
        block = re.search(
            r"(?ms)label > small\.muted,\s*\.hint\s*\{([^}]+)\}",
            css,
        )
        self.assertIsNotNone(block)
        self.assertIn("color: var(--muted-fg)", block.group(1))

    def test_settings_field_help_after_control(self):
        src = (ROOT / "app/web/templates/_settings_field.html").read_text(encoding="utf-8")
        # help must appear after the control close for text fields
        self.assertIn("</select>\n    {% if help %}<small class=\"muted\">{{ help }}</small>{% endif %}", src)
        self.assertRegex(src, r"<input name=\"s_\{\{ key \}\}\" value=\"\{\{ val \}\}\" />\s*\{% if help %\}")

    def test_no_escaped_muted_class_in_templates(self):
        bad = []
        needle = r'class=\"muted\"'
        for path in (ROOT / "app/web/templates").rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            if needle in text:
                bad.append(str(path.relative_to(ROOT)))
        self.assertEqual(bad, [], msg="broken escaped muted class breaks hint color/order CSS")

    def test_appearance_and_pwa_hints_below_upload(self):
        ap = (ROOT / "app/web/templates/_settings_appearance.html").read_text(encoding="utf-8")
        self.assertIn('class="muted"', ap)
        self.assertNotIn(r'class=\"muted\"', ap)
        self.assertIn("image-setting", ap)
        # Profile photo: upload box before the JPG caption
        self.assertRegex(
            ap,
            r'(?s)upload-box.*<small class="muted">فقط JPG — از API',
        )
        # Caption must not sit alone under the title before the upload control
        self.assertNotRegex(
            ap,
            r'(?s)<strong>عکس پروفایل</strong>\s*<small class="muted">',
        )

        pwa = (ROOT / "app/web/templates/_settings_pwa.html").read_text(encoding="utf-8")
        self.assertIn("image-setting", pwa)
        self.assertRegex(
            pwa,
            r'(?s)upload-box.*<small class="muted">خالی = لوگوی پنل',
        )
        self.assertNotRegex(
            pwa,
            r'(?s)<strong>آیکن وب‌اپ</strong>\s*<small class="muted">',
        )

    def test_css_cache_busted_by_app_version(self) -> None:
        from app.services.panel_assets import get_panel_asset_cache_key
        from test_panel_safe_speed import render_dashboard_shell

        html = render_dashboard_shell("home.html")
        self.assertRegex(html, r'href="/static/panel\.css\?v=[^" ]+"')
        self.assertIn(f'src="/static/panel.js?v={get_panel_asset_cache_key("panel.js")}"', html)
        self.assertIn(f'src="/static/panel_widget_request.js?v={get_panel_asset_cache_key("panel_widget_request.js")}"', html)
        self.assertIn(f'src="/static/panel_ok_flash.js?v={get_panel_asset_cache_key("panel_ok_flash.js")}"', html)

    def test_shop_settings_has_live_preview(self):
        shop = (ROOT / "app/web/templates/shop_settings.html").read_text(encoding="utf-8")
        self.assertIn('_tg_preview_appearance.html', shop)
        self.assertIn('_tg_preview_chat.html', shop)
        self.assertIn('_tg_preview_appearance_js.html', shop)
        self.assertIn('_tg_preview_chat_js.html', shop)
        admin = (ROOT / "app/web/templates/settings.html").read_text(encoding="utf-8")
        self.assertIn('_tg_preview_appearance.html', admin)
        self.assertIn('_tg_preview_chat.html', admin)
        # Shared partials exist
        for name in (
            "_tg_preview_appearance.html",
            "_tg_preview_chat.html",
            "_tg_preview_appearance_js.html",
            "_tg_preview_chat_js.html",
        ):
            self.assertTrue((ROOT / "app/web/templates" / name).is_file())

class DeleteButtonAndKebabTests(unittest.TestCase):
    def test_btn_danger_matches_soft_tint_style(self):
        css = CSS.read_text(encoding="utf-8")
        block = re.search(r"(?ms)^\.btn-danger\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("background: color-mix(in srgb, var(--destructive) 7%, transparent)", body)
        self.assertIn("color: var(--destructive-fg)", body)
        self.assertNotIn("0.92", body)

    def test_btn_ok_matches_soft_tint_style(self):
        css = CSS.read_text(encoding="utf-8")
        block = re.search(r"(?ms)^\.btn-ok\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        # Approve actions follow the panel brand colour like the other main actions.
        self.assertIn("background: color-mix(in srgb, var(--brand) 8%, transparent)", body)
        self.assertIn("color: var(--brand-fg)", body)

    def test_btn_warn_matches_soft_tint_style(self):
        css = CSS.read_text(encoding="utf-8")
        block = re.search(r"(?ms)^\.btn-warn\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("background: color-mix(in srgb, var(--warn) 7%, transparent)", body)
        self.assertIn("color: var(--warn-fg)", body)

    def test_modal_above_chrome(self):
        css = CSS.read_text(encoding="utf-8")
        block = re.search(r"(?ms)^\.ui-modal\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        self.assertIn("z-index: 4000", block.group(1))
        js = JS.read_text(encoding="utf-8")
        self.assertIn("document.body.appendChild(el)", js)
        self.assertIn("modalHomes", js)

    def test_select_has_up_down_chevron_opposite_title(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("M4.5 6L8 3l3.5 3", css)
        self.assertIn("M4.5 10L8 13l3.5-3", css)
        sel = re.search(r"(?ms)^select\s*\{([^}]+)\}", css)
        self.assertIsNotNone(sel)
        body = sel.group(1)
        # RTL: chevron on physical left (opposite the value text); inset matches title
        self.assertIn("background-position: left var(--space-2) center", body)
        self.assertIn("padding-inline-end: calc(var(--space-2) + 14px)", body)
        self.assertNotIn("background-position: right", body)
        self.assertIn("background-color: var(--control-bg)", body)
        # light theme must not wipe the chevron via background shorthand
        light = re.search(r'(?ms)html\[data-theme="light"\]\s+select\s*\{([^}]+)\}', css)
        self.assertIsNotNone(light)
        self.assertIn("background-image:", light.group(1))
        self.assertIn("background-position: left var(--space-2) center", light.group(1))
        # shared input padding must not force symmetric padding onto select
        shared = re.search(r"(?ms)^input,\s*select,\s*textarea\s*\{([^}]+)\}", css)
        self.assertIsNotNone(shared)
        self.assertNotRegex(shared.group(1), r"(?m)^\s*padding\s*:")

    def test_theme_caret_not_over_label(self):
        css = CSS.read_text(encoding="utf-8")
        caret = re.search(r"(?ms)^\.side-theme-caret\s*\{([^}]+)\}", css)
        self.assertIsNotNone(caret)
        body = caret.group(1)
        self.assertIn("margin-inline-start: auto", body)
        self.assertNotIn("position: absolute", body)
        self.assertNotIn("inset-inline-end", body)

    def test_kebab_covers_tablet_and_overflow(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 1100px)", css)
        self.assertIn(".table-wrap.force-kebab .row-actions-toggle", css)
        self.assertIn(".row-actions-menu.is-ported", css)
        # Ported kebab must keep a single vertical column even on wide viewports
        ported = re.search(r"(?ms)^\.row-actions-menu\.is-ported\s*\{([^}]+)\}", css)
        self.assertIsNotNone(ported)
        self.assertIn("flex-direction: column", ported.group(1))
        self.assertIn("flex-wrap: nowrap", ported.group(1))
        self.assertIn("max-height: none", ported.group(1))
        self.assertIn("overflow: visible", ported.group(1))
        self.assertNotIn("overflow-y: auto", ported.group(1))
        self.assertIn(".row-actions-menu.is-ported .row-actions-stack", css)
        self.assertIn(".row-actions-menu.is-ported select", css)
        js = JS.read_text(encoding="utf-8")
        self.assertIn("refreshForceKebab", js)
        self.assertIn("force-kebab", js)
        self.assertIn("is-ported", js)
        self.assertIn("rowMenuHomes", js)
        # Flip up when full height does not fit below; never clamp with scroll maxHeight
        self.assertIn("spaceAbove >= mh", js)
        self.assertIn("maxHeight = 'none'", js)
        self.assertNotIn("Math.min(mh, 140)", js)

    def test_update_warns_stay_on_page(self):
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(str(ROOT / "app/web/templates")), autoescape=True)
        html = env.get_template("_settings_update.html").render(
            update_info={"update_available": True, "remote_version": "9.9.9", "checked": True, "label": ""},
            status={"state": "running"},
            local_version="0.4.0",
        )
        self.assertIn("upd-stay-warn", html)
        self.assertIn("خارج نشوید", html)
        self.assertIn("رفرش نکنید", html)
        self.assertIn("چند دقیقه", html)
        self.assertNotIn("۲ تا ۳ دقیقه", html)
        # The leave guard lives in panel.js now, not in the partial.
        self.assertIn("beforeunload", JS.read_text(encoding="utf-8"))
        py = (ROOT / "app/services/panel_update.py").read_text(encoding="utf-8")
        self.assertIn("چند دقیقه", py)
        self.assertNotIn("۲ تا ۳ دقیقه", py)

    def test_modal_typography_locked(self):
        css = CSS.read_text(encoding="utf-8")
        panel = re.search(r"(?ms)^\.ui-modal-panel\s*\{([^}]+)\}", css)
        self.assertIsNotNone(panel)
        body = panel.group(1)
        self.assertIn("font-size: 14px", body)
        self.assertIn("text-size-adjust: 100%", body)
        self.assertIn("-webkit-text-size-adjust: 100%", body)
        self.assertIn(".ui-modal-panel p", css)
        self.assertIn(".ui-modal-panel label", css)
        self.assertIn(".ui-modal-head .icon-btn", css)
        # Close control must use SVG, not a raw × glyph that some fonts inflate
        for rel in (
            "app/web/templates/resellers.html",
            "app/web/templates/pg_users.html",
            "app/web/templates/_settings_backup.html",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn('data-modal-close', src)
            self.assertIn('class="ico"', src)
            self.assertNotRegex(src, r'data-modal-close[^>]*>\s*×\s*<')
        js = JS.read_text(encoding="utf-8")
        self.assertIn("ui-modal-panel", js)

    def test_site_footer_compact_sidebar_footer_restored(self):
        css = CSS.read_text(encoding="utf-8")
        foot = re.search(r"(?ms)^\.site-footer\s*\{([^}]+)\}", css)
        self.assertIsNotNone(foot)
        body = foot.group(1)
        self.assertIn("margin-top: auto", body)
        # Align with .side-foot baseline
        self.assertIn("padding-top: var(--page-title-gap)", body)
        # Same matte glass as modal sticky chrome
        self.assertIn("background: var(--modal-frost-bg);", body)
        self.assertIn("backdrop-filter: var(--modal-frost-blur);", body)
        self.assertIn("border-top: 1px solid var(--modal-frost-line);", body)
        # star button uses the compact tier, shorter than the primary --btn-h
        self.assertIn("min-height: var(--ctl-sm-h)", css)
        # sidebar footer must stay at the pre-compact sizing
        side = re.search(r"(?ms)^\.side-foot\s*\{([^}]+)\}", css)
        self.assertIsNotNone(side)
        side_body = side.group(1)
        self.assertIn("padding-top: var(--page-title-gap)", side_body)
        self.assertIn("background: var(--modal-frost-bg);", side_body)
        self.assertIn("backdrop-filter: var(--modal-frost-blur);", side_body)
        logout = re.search(r"(?ms)^\.logout-link\s*\{([^}]+)\}", css)
        self.assertIsNotNone(logout)
        self.assertIn("font-size: 13px", logout.group(1))
        self.assertIn("height: var(--ctl-sm-h)", logout.group(1))
        self.assertIn("padding: 0 var(--space-1)", logout.group(1))

    def test_shell_chrome_matches_modal_frost_and_brand_menu(self):
        """Mobile topbar / drawer use the same frost tokens; menu follows brand colour."""
        css = CSS.read_text(encoding="utf-8")
        # Prefer the fixed chrome block (not the short `.topbar { direction: ltr; }`)
        top = re.search(
            r"(?ms)^\.topbar\s*\{([^}]*background:\s*var\(--modal-frost-bg\);[^}]*)\}",
            css,
        )
        self.assertIsNotNone(top)
        top_body = top.group(1)
        self.assertIn("backdrop-filter: var(--modal-frost-blur);", top_body)
        self.assertIn("border-bottom: 1px solid var(--modal-frost-line);", top_body)
        self.assertIn("var(--topbar-pad-bottom)", top_body)
        self.assertIn("padding: var(--safe-top) var(--space-2) var(--topbar-pad-bottom);", top_body)
        self.assertNotIn("topbar-pad-top", top_body)
        self.assertIn("--topbar-pad-bottom: var(--space-1_5);", css)
        self.assertIn("--topbar-content-h: 58px;", css)
        menu = re.search(r"(?ms)^\.menu-toggle\s*\{([^}]+)\}", css)
        self.assertIsNotNone(menu)
        menu_body = menu.group(1)
        self.assertIn("border-radius: var(--control-radius);", menu_body)
        self.assertIn("height: var(--btn-h);", menu_body)
        self.assertIn("min-height: var(--btn-h);", menu_body)
        self.assertIn("color: var(--brand-fg);", menu_body)
        self.assertIn(
            "border: 1.5px solid color-mix(in srgb, var(--brand) 72%, transparent);",
            menu_body,
        )
        self.assertIn(
            "background: color-mix(in srgb, var(--brand) 8%, transparent);",
            menu_body,
        )
        self.assertNotIn("var(--bot-line)", menu_body)
        self.assertNotIn("var(--r-sm)", menu_body)
        self.assertRegex(
            css,
            r"(?ms)\.side-panel\s*\{[^}]*backdrop-filter:\s*var\(--modal-frost-blur\);",
        )

    def test_block_button_is_warn_update_is_ok(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("btn-warn", users)
        inbox = (ROOT / "app/web/templates/inbox.html").read_text(encoding="utf-8")
        self.assertRegex(inbox, r'btn-ok[^>]*>\s*آپدیت\s*<')
        upd = (ROOT / "app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn('id="upd-start"', upd)
        self.assertIn("btn-ok", upd)

    def test_pg_admins_uses_row_actions_macro(self):
        src = (ROOT / "app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("row_actions", src)
        self.assertIn("{% call row_actions() %}", src)
        self.assertIn("btn-danger", src)
        self.assertIn("row-actions-toggle", (ROOT / "app/web/templates/macros.html").read_text(encoding="utf-8"))

    def test_pg_templates_uses_kebab(self):
        src = (ROOT / "app/web/templates/pg_templates.html").read_text(encoding="utf-8")
        self.assertIn("{% call row_actions() %}", src)
        self.assertIn("btn-danger", src)

    def test_common_delete_buttons_are_danger(self):
        for rel in (
            "app/web/templates/pg_users.html",
            "app/web/templates/plans.html",
            "app/web/templates/_settings_backup.html",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("btn-danger", src)
            # Ignore wholesale/PAYG tier-remove ghost buttons (not destructive deletes)
            cleaned = re.sub(
                r'btn-ghost[^>]*data-tier-remove[^>]*>\s*حذف\s*<',
                "",
                src,
            )
            self.assertNotRegex(cleaned, r'btn-ghost[^>]*>\s*حذف\s*<')


class HtmlCommentBalanceGuardTests(unittest.TestCase):
    """An HTML comment closed the wrong way (e.g. a leftover JS-style `*/`
    instead of `-->`) makes the browser swallow the *entire rest of the
    document* into one giant unterminated comment — base.html alone breaking
    this way once meant every page in the panel silently rendered empty.
    Guard every template against ever regressing that class of bug again."""

    def _find_unterminated(self, text: str) -> list[int]:
        unterminated = []
        idx = 0
        while True:
            start = text.find("<!--", idx)
            if start == -1:
                break
            end = text.find("-->", start + 4)
            if end == -1:
                unterminated.append(start)
                break
            idx = end + 3
        return unterminated

    def test_no_unterminated_html_comments_in_any_template(self):
        templates_dir = ROOT / "app/web/templates"
        offenders = []
        for path in sorted(templates_dir.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for start in self._find_unterminated(text):
                line_no = text.count("\n", 0, start) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}")
        self.assertEqual(
            offenders,
            [],
            msg=(
                "Unterminated HTML comment(s) (missing '-->') would swallow "
                f"the rest of the document as plain comment text: {offenders}"
            ),
        )

    def test_base_html_has_meta_theme_color_and_ios_safari(self):
        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn('id="meta-theme-color"', base)
        self.assertNotIn("--vvh", base)


if __name__ == "__main__":
    unittest.main()
