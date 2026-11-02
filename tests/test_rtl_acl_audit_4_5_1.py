"""RTL/Persian UI guards + plans→PG template ACL bypass fix."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")


class RtlPlaceholderTests(unittest.TestCase):
    def test_placeholder_right_aligned_and_theme_muted(self):
        """Placeholders (examples) are always RTL + right + theme --placeholder."""
        self.assertIn("color: var(--placeholder);", CSS)
        self.assertIn("--placeholder:", CSS)
        block_start = CSS.find("input::placeholder,\ntextarea::placeholder {")
        self.assertGreaterEqual(block_start, 0)
        block = CSS[block_start : block_start + 220]
        self.assertIn("text-align: right;", block)
        self.assertIn("direction: rtl;", block)
        self.assertIn('input[dir="ltr"]::placeholder', CSS)
        self.assertIn('input[dir="ltr"],\ntextarea[dir="ltr"]', CSS)
        # Hardcoded dark-only grey must not return (breaks light theme mute).
        self.assertNotIn(
            "input::placeholder,\ntextarea::placeholder {\n  color: #52525b;",
            CSS,
        )


class RtlLtrHotspotsTests(unittest.TestCase):
    def test_login_fields_ltr(self):
        src = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
        self.assertIn('name="username"', src)
        self.assertIn('dir="ltr"', src)
        self.assertIn('name="password"', src)

    def test_auth_form_inputs_right_aligned(self):
        self.assertIn(".auth-form input[dir=\"ltr\"]", CSS)
        block = CSS.split("Login / setup auth boxes", 1)[1].split("CRITICAL:", 1)[0]
        self.assertIn("text-align: right;", block)
        self.assertIn(".auth-form .pw-field input", block)

    def test_hosts_users_nodes_ltr(self):
        hosts = (ROOT / "app/web/templates/pg_hosts.html").read_text(encoding="utf-8")
        users = (ROOT / "app/web/templates/pg_users.html").read_text(encoding="utf-8")
        nodes = (ROOT / "app/web/templates/pg_nodes.html").read_text(encoding="utf-8")
        self.assertIn('dir="ltr"', hosts)
        # Usernames and node ids stay left-to-right; the cell may carry more classes.
        self.assertRegex(users, r'class="mono[^"]*" dir="ltr"')
        self.assertRegex(nodes, r'class="mono[^"]*" dir="ltr"')

    def test_resellers_table_has_no_commission_column(self):
        src = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertNotIn("commission_percent", src)
        self.assertNotIn("کمیسیون", src)

    def test_payg_rate_isolates_amount(self):
        src = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(encoding="utf-8")
        # Rate amount uses num-ratio (RTL presentation); unit «/ گیگ» stays beside it
        self.assertIn('class="num-ratio"', src)
        self.assertIn("/ گیگ", src)


class PlansTemplateCreateAclTests(unittest.TestCase):
    def test_helper_requires_exact_create_action(self):
        from app.services.plans_catalog import staff_can_create_pg_template

        reseller = {
            "role": "reseller",
            "pg_writes": {"templates": True},
            "pg_actions": {"templates": {"create": False, "update": True, "delete": False}},
        }
        self.assertFalse(staff_can_create_pg_template(reseller))
        reseller["pg_actions"]["templates"]["create"] = True
        self.assertTrue(staff_can_create_pg_template(reseller))

    def test_plans_create_uses_staff_pg_not_owner_token(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        block = src.split("if also_create_template:", 1)[1].split("from app.services.orders import parse_naming_form", 1)[0]
        self.assertIn("_staff_pg", block)
        self.assertIn("create_user_template", block)
        self.assertNotIn("get_pg().create_user_template", block)
        self.assertIn("staff_can_create_pg_template", block)


class PgMutationServerAclTests(unittest.TestCase):
    """Sensitive /pg write routes must gate on staff_pg_action / user actions."""

    def test_hosts_templates_edit_check_update(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn('staff_pg_action(staff, "hosts", "update")', src)
        self.assertIn('staff_pg_action(staff, "templates", "update")', src)
        self.assertIn('@app.post("/pg/hosts/{host_id}/edit")', src)
        self.assertIn('@app.post("/pg/templates/{template_id}/edit")', src)

    def test_no_owner_token_in_staff_template_create_on_plans(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        # Global get_pg().create_user_template must not appear after also_create_template fix
        self.assertNotIn("await get_pg().create_user_template(", src)


if __name__ == "__main__":
    unittest.main()
