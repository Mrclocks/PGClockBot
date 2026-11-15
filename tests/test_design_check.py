"""The design ratchet finds the checkable design-rule violations in templates and stylesheets."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import design_check
from scripts.design_check import css_findings, findings_for, template_findings


def rules(findings) -> list[str]:
    return sorted(f.rule for f in findings)


class TemplateRuleTests(unittest.TestCase):
    def test_a_modal_outside_the_design_system_is_found(self) -> None:
        self.assertEqual(rules(template_findings('<div class="ui-modal" id="m"></div>')), ["modal-not-ds"])

    def test_conditional_classes_are_read_without_their_jinja_tags(self) -> None:
        html = '<div class="ui-modal ds{% if open %} open{% endif %}" id="m"><div class="ui-modal-panel"></div></div>'
        self.assertEqual(template_findings(html), [])

    def test_a_cancel_button_in_a_form_modal_is_found(self) -> None:
        html = ('<div class="ui-modal ds" id="m"><form><div class="ds-foot">'
                '<button class="btn">ذخیره</button><button type="button" class="btn btn-ghost" data-modal-close>انصراف</button>'
                '</div></form></div>')
        self.assertEqual(rules(template_findings(html)), ["form-modal-cancel"])

    def test_a_dialog_keeps_its_cancel_button(self) -> None:
        html = ('<div class="ui-modal ds ds-dialog" id="d"><form>'
                '<button type="button" class="btn btn-ghost" data-modal-close>انصراف</button></form></div>')
        self.assertEqual(template_findings(html), [])

    def test_the_header_close_button_is_not_a_cancel_button(self) -> None:
        html = ('<div class="ui-modal ds" id="m"><div class="ui-modal-head">'
                '<button type="button" class="icon-btn" data-modal-close aria-label="بستن"></button></div></div>')
        self.assertEqual(template_findings(html), [])

    def test_a_footer_macro_with_cancel_counts_only_outside_a_dialog(self) -> None:
        form_modal = '<div class="ui-modal ds" id="m">\n{{ ds_foot(\'ذخیره\', cancel=true) }}\n</div>'
        dialog = '<div class="ui-modal ds ds-dialog" id="d">\n{{ ds_foot(\'حذف\', cancel=true) }}\n</div>'
        self.assertEqual(rules(template_findings(form_modal)), ["form-modal-cancel"])
        self.assertEqual(template_findings(dialog), [])

    def test_the_panel_inside_a_dialog_does_not_hide_the_dialog(self) -> None:
        dialog = ('<div class="ui-modal ds ds-dialog" id="d">\n<div class="ui-modal-panel" role="dialog">\n'
                  '<form>\n{{ ds_foot(\'ابطال\', cancel=true) }}\n</form>\n</div>\n</div>')
        self.assertEqual(template_findings(dialog), [])
        form_modal = dialog.replace(" ds-dialog", "")
        self.assertEqual(rules(template_findings(form_modal)), ["form-modal-cancel"])

    def test_inline_styles_are_found(self) -> None:
        self.assertEqual(rules(template_findings('<p style="margin-top: 7px">x</p>')), ["inline-style"])

    def test_a_layout_transition_is_found_and_compositor_motion_is_not(self) -> None:
        self.assertEqual(rules(css_findings(".bar i { transition: width 0.3s ease; }")), ["layout-motion"])
        self.assertEqual(rules(css_findings(".dock { transition: right var(--motion-med); }")), ["layout-motion"])
        smooth = ".fill { transition: clip-path var(--motion-med), opacity var(--motion-fast), transform 1s; }"
        self.assertEqual(css_findings(smooth), [])
        self.assertEqual(css_findings(".x { transition: border-color var(--motion-fast); }"), [])

    def test_a_raw_gap_is_found_and_a_token_gap_is_not(self) -> None:
        self.assertEqual(rules(css_findings(".grid { gap: 12px; }")), ["raw-gap"])
        self.assertEqual(rules(css_findings(".grid { row-gap: 10px; column-gap: var(--g4); }")), ["raw-gap"])
        self.assertEqual(css_findings(".grid { gap: var(--section-gap); }"), [])
        self.assertEqual(css_findings(".grid { gap: var(--space-1, 0.25rem) 0; }"), [])

    def test_a_custom_property_that_refers_to_itself_is_found(self) -> None:
        self.assertEqual(rules(css_findings(":root {\n  --ok-solid: var(--ok-solid);\n}")), ["token-loop"])
        self.assertEqual(rules(css_findings(":root {\n  --a: var(--a, #fff);\n}")), ["token-loop"])
        self.assertEqual(css_findings(":root {\n  --ok-solid: #16a34a;\n  --tone: var(--ok-solid);\n}"), [])

    def test_a_byte_order_mark_in_a_template_is_found(self) -> None:
        self.assertEqual(rules(template_findings('{% from "x.html" import y %}\n\ufeff{% extends "base.html" %}')), ["bom"])
        self.assertEqual(template_findings('{% extends "base.html" %}'), [])

    def test_a_hand_drawn_icon_is_found_but_a_data_graphic_is_not(self) -> None:
        icon = '<svg class="ico" viewBox="0 0 24 24"><path d="M6 6l12 12"/></svg>'
        small = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8"/></svg>'
        gauge = '<svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="52"/></svg>'
        self.assertEqual(rules(template_findings(icon)), ["inline-svg"])
        self.assertEqual(rules(template_findings(small)), ["inline-svg"])
        self.assertEqual(template_findings(gauge), [])

    def test_a_style_that_only_passes_data_to_css_is_allowed(self) -> None:
        self.assertEqual(template_findings('<span style="--tag-color: {{ tag.hex }}">x</span>'), [])
        self.assertEqual(template_findings('<i style="--p: {{ pct }}; --w: 4">x</i>'), [])
        mixed = '<i style="--p: {{ pct }}; width: 40%">x</i>'
        self.assertEqual(rules(template_findings(mixed)), ["inline-style"])

    def test_hand_written_svg_is_found_outside_the_icon_set(self) -> None:
        svg = '<span><svg viewBox="0 0 24 24"><path d="M0 0"/></svg></span>'
        self.assertEqual(rules(findings_for("app/web/templates/page.html", svg)), ["inline-svg"])
        self.assertEqual(findings_for("app/web/templates/_icons.html", svg), [])


class StylesheetRuleTests(unittest.TestCase):
    def test_literal_colours_in_rules_are_found(self) -> None:
        css = ".x { color: #ff0000; border: 1px solid #abc; }"
        self.assertEqual(rules(css_findings(css)), ["raw-colour", "raw-colour"])

    def test_literal_text_sizes_are_found_and_scale_tokens_are_not(self) -> None:
        css = ".a { font-size: 13px; }\n.b { font: 600 11px/1 monospace; }\n.c { font-size: var(--fs-2); }"
        self.assertEqual(rules(css_findings(css)), ["raw-font-size", "raw-font-size"])

    def test_scale_definitions_may_hold_literal_sizes(self) -> None:
        self.assertEqual(css_findings(":root {\n  --fs-1: 11px;\n  --radius: 20px;\n}"), [])

    def test_literal_radii_are_found_but_zero_and_percent_are_not(self) -> None:
        css = ".a { border-radius: 12px; }\n.b { border-top-left-radius: 0; }\n.c { border-radius: 50%; }\n.d { border-radius: var(--radius); }"
        self.assertEqual(rules(css_findings(css)), ["raw-radius"])

    def test_important_is_found(self) -> None:
        self.assertEqual(rules(css_findings(".a { color: var(--x) !important; }")), ["important"])

    def test_tokens_and_comments_may_hold_colours(self) -> None:
        css = ":root {\n  --brand: #f97316;\n}\n/* was #000 */\n.x { color: var(--brand); }"
        self.assertEqual(css_findings(css), [])


class ScopeTests(unittest.TestCase):
    def test_only_panel_templates_and_stylesheets_are_checked(self) -> None:
        self.assertEqual(findings_for("app/services/x.py", '<div class="ui-modal">'), [])
        self.assertEqual(findings_for("app/web/static/guide/a.css", ".x { color: #000; }"), [])
        self.assertEqual(rules(findings_for("app/web/static/panel-ds.css", ".x { color: #000; }")), ["raw-colour"])


if __name__ == "__main__":
    unittest.main()


class StrictRuleTests(unittest.TestCase):
    """Rules the panel fully meets fail on any finding in any tracked file, not only on growth."""

    def test_every_strict_rule_is_a_known_rule(self) -> None:
        self.assertLessEqual(design_check.STRICT_RULES, set(design_check.RULE_HELP))

    def test_one_finding_anywhere_is_reported_even_in_an_unchanged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app/web/templates").mkdir(parents=True)
            (root / "app/web/templates/old.html").write_text('<p style="margin: 0">x</p>', encoding="utf-8")
            (root / "app/web/templates/fine.html").write_text('<p class="muted">x</p>', encoding="utf-8")
            files = "app/web/templates/old.html\napp/web/templates/fine.html\n"
            with patch.object(design_check, "ROOT", root), patch.object(design_check, "_git", return_value=files):
                found = design_check.strict_findings()
        self.assertEqual([(path, f.rule) for path, f in found], [("app/web/templates/old.html", "inline-style")])

    def test_the_panel_meets_every_strict_rule_today(self) -> None:
        self.assertEqual(design_check.strict_findings(), [])
