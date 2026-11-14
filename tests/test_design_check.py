"""The design ratchet finds the checkable design-rule violations in templates and stylesheets."""

from __future__ import annotations

import unittest

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

    def test_inline_styles_are_found(self) -> None:
        self.assertEqual(rules(template_findings('<p style="margin-top: 7px">x</p>')), ["inline-style"])

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
