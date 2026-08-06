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
        self.assertIn("text-align: right", body)
        self.assertIn("direction: rtl", body)

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

    def test_css_cache_busted_by_app_version(self):
        src = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn('href="/static/panel.css?v={{ app_version }}"', src)
        self.assertIn('src="/static/panel.js?v={{ app_version }}"', src)

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
        self.assertIn("background: rgba(239, 68, 68, 0.12)", body)
        self.assertIn("color: var(--destructive-fg)", body)
        self.assertNotIn("0.92", body)

    def test_btn_ok_matches_soft_tint_style(self):
        css = CSS.read_text(encoding="utf-8")
        block = re.search(r"(?ms)^\.btn-ok\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("background: rgba(34, 197, 94, 0.12)", body)
        self.assertIn("color: var(--ok-fg)", body)

    def test_btn_warn_matches_soft_tint_style(self):
        css = CSS.read_text(encoding="utf-8")
        block = re.search(r"(?ms)^\.btn-warn\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("background: rgba(234, 179, 8, 0.12)", body)
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
        upd = (ROOT / "app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn("upd-stay-warn", upd)
        self.assertIn("خارج نشوید", upd)
        self.assertIn("رفرش نکنید", upd)
        self.assertIn("چند دقیقه", upd)
        self.assertNotIn("۲ تا ۳ دقیقه", upd)
        self.assertIn("beforeunload", upd)
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
        # star button should be shorter than primary --btn-h
        self.assertIn("min-height: 28px", css)
        # sidebar footer must stay at the pre-compact sizing
        side = re.search(r"(?ms)^\.side-foot\s*\{([^}]+)\}", css)
        self.assertIsNotNone(side)
        self.assertIn("padding-top: var(--page-title-gap)", side.group(1))
        logout = re.search(r"(?ms)^\.logout-link\s*\{([^}]+)\}", css)
        self.assertIsNotNone(logout)
        self.assertIn("font-size: 13px", logout.group(1))
        self.assertIn("height: 28px", logout.group(1))
        self.assertIn("padding: 0 var(--space-1)", logout.group(1))

    def test_block_button_is_warn_update_is_ok(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("btn-warn", users)
        home = (ROOT / "app/web/templates/home.html").read_text(encoding="utf-8")
        self.assertRegex(home, r'btn-ok[^>]*>\s*آپدیت\s*<')
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
            # Ignore JS-built markup (e.g. wholesale tier removers).
            html = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", src)
            self.assertNotRegex(
                html,
                r"<button[^>]*\bbtn-ghost\b[^>]*>\s*حذف\s*</button>",
            )


if __name__ == "__main__":
    unittest.main()
