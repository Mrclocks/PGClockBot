"""Panel UI fixes — volume spacing, Owner kebab, group modal, digits, switches."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.formatting import format_bytes, format_bytes_ratio
from app.services.numbers import normalize_number_text, parse_float, parse_int
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]
GB = 1024**3


class VolumeSpacingTests(unittest.TestCase):
    def test_ratio_spaces_around_slash(self):
        text = format_bytes_ratio(int(1.12 * GB), 10 * GB)
        self.assertEqual(text, "1.12 / 10 گیگ")
        self.assertTrue(text.index("1.12") < text.index("گیگ"))
        self.assertNotRegex(text, r"گیگ\s+\d")

    def test_single_value_number_then_unit(self):
        text = format_bytes(10 * GB)
        self.assertEqual(text, "10 گیگ")
        self.assertFalse(text.startswith("گیگ"))


class OwnerNoActionsTests(unittest.TestCase):
    def test_owner_row_skips_row_actions(self):
        src = (ROOT / "app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("{% if src != 'owner' %}", src)
        # Owner branch must not call row_actions
        owner_block = src.split("{% elif src == 'owner' %}", 1)[1].split("{% elif src == 'pg_staff' %}", 1)[0]
        self.assertNotIn("row_actions", owner_block)
        self.assertNotIn("ویرایش", owner_block)


class GroupEditModalTests(unittest.TestCase):
    def test_edit_is_modal_not_page_card(self):
        src = (ROOT / "app/web/templates/pg_groups.html").read_text(encoding="utf-8")
        self.assertIn('id="modal-pg-group-edit"', src)
        self.assertIn('data-modal-open="modal-pg-group-edit"', src)
        self.assertNotIn('href="/pg/groups?edit=', src)
        self.assertNotIn("{% if edit_group and can_write %}\n<div class=\"card\">", src)
        edit = src.split('id="modal-pg-group-edit"', 1)[1]
        self.assertNotIn('data-modal-close>انصراف</button>', edit)


class HostsTemplatesEditTests(unittest.TestCase):
    def test_hosts_edit_ui_and_route(self):
        html = (ROOT / "app/web/templates/pg_hosts.html").read_text(encoding="utf-8")
        py = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn('data-modal-open="modal-pg-host-edit"', html)
        self.assertIn('id="modal-pg-host-edit"', html)
        self.assertIn('can_update', html)
        self.assertIn('@app.post("/pg/hosts/{host_id}/edit")', py)
        self.assertIn("modify_host", py)

    def test_templates_edit_ui_and_route(self):
        html = (ROOT / "app/web/templates/pg_templates.html").read_text(encoding="utf-8")
        py = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn('data-modal-open="modal-pg-template-edit"', html)
        self.assertIn('id="modal-pg-template-edit"', html)
        self.assertIn('@app.post("/pg/templates/{template_id}/edit")', py)
        self.assertIn("modify_user_template", py)


class SwitchHoverThemeTests(unittest.TestCase):
    def test_no_hardcoded_black_hover(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertNotIn(".ui-switch-row:hover,\n.toggle-row:hover,\n.notify-row:hover {\n  background: #101014;", css)
        self.assertNotIn(".ui-switch-chip:hover {\n  background: #101014;", css)
        self.assertIn("html[data-theme=\"light\"] .ui-switch-chip:hover", css)
        self.assertIn(".ui-switch-row:hover", css)
        self.assertIn("var(--bg-hover", css)


class CancelButtonUnityTests(unittest.TestCase):
    def test_ghost_link_matches_button(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        # One rule serves both themes: the neutral edge and fill are mixed from --foreground.
        self.assertIn("a.btn.btn-ghost:hover", css)
        self.assertNotIn('html[data-theme="light"] a.btn.btn-ghost', css)

    def test_plan_form_modals_have_no_cancel_button(self):
        plans = template_with_includes("plans.html")
        self.assertNotIn('data-modal-close>انصراف</button>', plans)


class DigitNormalizeTests(unittest.TestCase):
    def test_persian_and_arabic(self):
        self.assertEqual(normalize_number_text("۱۲۳"), "123")
        self.assertEqual(normalize_number_text("١٢٣"), "123")
        self.assertEqual(parse_int("۵۰"), 50)
        self.assertAlmostEqual(parse_float("۱٫۵"), 1.5)
        self.assertAlmostEqual(parse_float("١٫١٢"), 1.12)

    def test_panel_js_has_normalizer(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("normalizePanelNumberText", js)
        self.assertIn("unlockNumberInputs", js)
        self.assertIn("wasNumber", js)


class RowHeightStabilityTests(unittest.TestCase):
    def test_default_kebab_stable_and_fixed_actions(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("allow-inline-actions", css)
        self.assertIn("allow-inline-actions", js)
        self.assertIn("td.col-actions", css)
        self.assertIn("max-height: var(--btn-h)", css)
        # Default in-cell menu inert (not only under force-kebab)
        self.assertGreaterEqual(css.count(".row-actions-menu:not(.is-ported) {"), 2)


if __name__ == "__main__":
    unittest.main()
