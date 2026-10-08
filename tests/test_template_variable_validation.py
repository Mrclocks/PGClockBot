"""Field-specific template feedback, role filtering and safe panel metadata."""

from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from app.services import message_variables as variables
from app.services.daily_report import ACTOR_OWNER, ACTOR_SHOP, metrics_for_actor
from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = ""
        self.script_attrs = {}
        self.in_metadata = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "template-variable-specs":
            self.in_metadata = True
            self.script_attrs = attrs

    def handle_data(self, data):
        if self.in_metadata:
            self.metadata += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_metadata = False


class TemplateVariableSpecsTests(unittest.TestCase):
    def setUp(self):
        self.specs = variables.template_variable_specs()

    def test_all_template_fields_have_specs(self):
        self.assertEqual(set(self.specs), set(variables.SETTING_DOMAIN))
        self.assertTrue(all(self.specs.values()))
        self.assertIn("psp_pay_text", self.specs)
        self.assertIn("custom_plan_username_pattern", self.specs)
        for group in SETTING_GROUPS.values():
            for key, _label, kind, *extra in group:
                if kind in {"text", "textarea"} and extra and "متغیر" in extra[0] and PLACEHOLDER.search(extra[0]):
                    with self.subTest(key=key):
                        self.assertIn(key, self.specs)

    def test_default_templates_only_use_supported_names(self):
        for key, template in DEFAULT_SETTINGS.items():
            names = set(PLACEHOLDER.findall(template))
            if not names:
                continue
            with self.subTest(key=key):
                self.assertIn(key, self.specs)
                self.assertLessEqual(names, set(self.specs[key]))

    def test_specs_never_extend_renderer_domains(self):
        for key, domain in variables.SETTING_DOMAIN.items():
            with self.subTest(key=key):
                self.assertLessEqual(set(self.specs[key]), variables.allowed_keys_for_domain(domain))

    def test_payment_fields_have_distinct_contexts(self):
        self.assertEqual(set(self.specs["gateway_link"]), {"amount", "order_id", "payment_id"})
        self.assertEqual(set(self.specs["card_pay_text"]), {"amount", "card", "holder", "payment_id", "shop_title"})
        self.assertIn("address", self.specs["crypto_pay_text"])
        self.assertNotIn("address", self.specs["card_pay_text"])
        self.assertNotIn("order_id", self.specs["crypto_pay_text"])
        self.assertNotIn("gateway_name", self.specs["psp_pay_text"])
        self.assertIn("order_id", self.specs["psp_pay_text"])

    def test_aliases_are_resolved_per_field(self):
        self.assertEqual(self.specs["welcome_text"]["name"], self.specs["welcome_text"]["user_name"])
        self.assertEqual(self.specs["gateway_pay_text"]["name"], self.specs["gateway_pay_text"]["gateway_name"])
        self.assertNotEqual(self.specs["welcome_text"]["name"], self.specs["gateway_pay_text"]["name"])
        self.assertEqual(set(self.specs["qr_caption"]), {"url", "sub_link"})
        self.assertIn("sub_link", self.specs["purchase_success_text"])
        self.assertNotIn("name", self.specs["card_pay_text"])

    def test_catalog_entry_without_field_context_is_not_valid(self):
        self.assertIn("bot_username", variables.allowed_keys_for_domain(variables.DOMAIN_USER))
        self.assertNotIn("bot_username", self.specs["welcome_text"])
        self.assertNotIn("bot_username", self.specs["referral_text"])
        self.assertEqual(set(self.specs["force_join_msg"]), {"channels"})

    def test_daily_report_specs_match_actor_metrics(self):
        for actor in (ACTOR_OWNER, ACTOR_SHOP):
            with self.subTest(actor=actor):
                specs = variables.template_variable_specs(actor)
                self.assertEqual(set(specs["admin_daily_report_template"]), {m.key for m in metrics_for_actor(actor)})
        shop = variables.template_variable_specs(ACTOR_SHOP)
        self.assertNotIn("resellers_active", shop["admin_daily_report_template"])
        self.assertIn("resellers_active", self.specs["admin_daily_report_template"])
        self.assertIn("orders_new", shop["admin_daily_report_template"])

    def test_unknown_actor_does_not_receive_owner_only_metrics(self):
        self.assertNotIn("resellers_active", variables.template_variable_specs("unknown")["admin_daily_report_template"])

    def test_all_naming_fields_follow_catalog_extensions(self):
        new_var = variables.MessageVar(
            key="future_plan_value", title_fa="Future", description_fa="", example="",
            domains=frozenset({variables.DOMAIN_NAMING}), aliases=("future_alias",),
        )
        with patch.object(variables, "_VARS", (*variables._VARS, new_var)):
            specs = variables.template_variable_specs(ACTOR_SHOP)
            expected = variables.allowed_keys_for_domain(variables.DOMAIN_NAMING)
            for key in ("pg_username_pattern", "custom_plan_username_pattern"):
                self.assertEqual(set(specs[key]), expected)
                self.assertIn("future_plan_value", specs[key])
                self.assertIn("future_alias", specs[key])

    def test_naming_buyer_and_volume_vars_in_catalog_specs_and_page(self):
        naming = {"plan_volume", "plan_unit", "username", "id", "prefix", "random", "suffix"}
        self.assertTrue(naming <= variables.allowed_keys_for_domain(variables.DOMAIN_NAMING))
        for key in ("pg_username_pattern", "custom_plan_username_pattern"):
            self.assertTrue(naming <= set(self.specs[key]))
        groups = {g["domain"]: g for g in variables.catalog_groups()}
        self.assertIn(variables.DOMAIN_NAMING, groups)
        page_keys = {item["key"] for item in groups[variables.DOMAIN_NAMING]["vars"]}
        self.assertTrue({"plan_volume", "plan_unit", "username"} <= page_keys)
        # Caption chips on naming textareas must accept the new tokens as valid.
        built = variables.render_message_template(
            "{username}_{plan_volume}{plan_unit}_{id}",
            domain=variables.DOMAIN_NAMING,
            username="ali",
            plan_volume="30",
            plan_unit="GB",
            id="1024",
            html=False,
        )
        self.assertEqual(built, "ali_30GB_1024")


class TemplateVariablePanelTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=True)
        self.env.globals["template_variable_specs"] = variables.template_variable_specs
        self.context = {
            "staff": {"role": "admin"},
            "request": SimpleNamespace(url=SimpleNamespace(path="/settings"), query_params={}),
            "app_version": "test", "csp_nonce": "test-nonce",
        }

    def metadata(self, **context):
        rendered = self.env.get_template("base.html").render({**self.context, **context})
        parser = MetadataParser()
        parser.feed(rendered)
        return rendered, parser

    def test_base_includes_nonce_protected_metadata_and_shared_script(self):
        rendered, parser = self.metadata()
        self.assertEqual(parser.script_attrs["type"], "application/json")
        self.assertEqual(parser.script_attrs["nonce"], "test-nonce")
        self.assertEqual(json.loads(parser.metadata), variables.template_variable_specs())
        self.assertIn('/static/template-variables.js?v=test" nonce="test-nonce"', rendered)

    def test_shop_context_and_explicit_report_actor(self):
        for context in ({"shop_mode": True}, {"daily_report_actor": ACTOR_SHOP}):
            with self.subTest(context=context):
                _, parser = self.metadata(**context)
                self.assertNotIn("resellers_active", json.loads(parser.metadata)["admin_daily_report_template"])

    def test_metadata_cannot_close_script_or_inject_html(self):
        value = '</script><img src=x onerror="alert(1)">'
        self.env.globals["template_variable_specs"] = lambda actor: {"welcome_text": {"name": value}}
        rendered, parser = self.metadata()
        self.assertNotIn(value, rendered)
        self.assertEqual(json.loads(parser.metadata)["welcome_text"]["name"], value)

    def test_auth_pages_do_not_load_field_validation(self):
        rendered, parser = self.metadata(staff=None)
        self.assertEqual(parser.metadata, "")
        self.assertNotIn("/static/template-variables.js", rendered)

    def test_feedback_uses_safe_dom_and_handles_dynamic_fields(self):
        script = (ROOT / "app/web/static/template-variables.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("setCustomValidity", script)
        self.assertNotIn("preventDefault", script)
        self.assertIn("pay-dest-gw-link", script)
        self.assertIn("MutationObserver", script)
        self.assertIn("panel:dom-ready", script)
        self.assertIn("aria-describedby", script)
        self.assertIn("aria-live", script)

    def test_variable_tokens_are_capsule_chips(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        token = css.split(".template-variable-token {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 999px;", token)
        self.assertNotIn("border-radius: 4px;", token)


if __name__ == "__main__":
    unittest.main()
