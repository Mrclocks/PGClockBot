"""Pre-merge authorization regression: /pg/* writes + role matrix + RTL payload safety."""
from __future__ import annotations

import ast
import pathlib
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pasarguard import PasarGuardError
from app.services.pg_access import staff_pg_action, staff_user_actions
from app.services.plans_catalog import staff_can_create_pg_template


ROOT = pathlib.Path(__file__).resolve().parents[1]
PG_PAGES = (ROOT / "app" / "api" / "pg_pages.py").read_text(encoding="utf-8")
APP_PY = (ROOT / "app" / "api" / "app.py").read_text(encoding="utf-8")
PLAN_PAGES_PY = (ROOT / "app" / "api" / "plan_pages.py").read_text(encoding="utf-8")
PANEL_JS = (ROOT / "app" / "web" / "static" / "panel.js").read_text(encoding="utf-8")
PANEL_CSS = (ROOT / "app" / "web" / "static" / "panel.css").read_text(encoding="utf-8")


def _pg_write_route_blocks() -> list[tuple[str, str]]:
    """Return [(path, function_source)] for POST/PUT/DELETE/PATCH under /pg."""
    tree = ast.parse(PG_PAGES)
    lines = PG_PAGES.splitlines()
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "register_pg_pages":
            continue
        for child in ast.walk(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in child.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if not isinstance(dec.func, ast.Attribute):
                    continue
                method = dec.func.attr.lower()
                if method not in {"post", "put", "delete", "patch"}:
                    continue
                if not dec.args:
                    continue
                arg0 = dec.args[0]
                if not isinstance(arg0, ast.Constant) or not isinstance(arg0.value, str):
                    continue
                path = arg0.value
                if not path.startswith("/pg"):
                    continue
                start = child.lineno - 1
                end = child.end_lineno or child.lineno
                src = "\n".join(lines[start:end])
                out.append((path, src))
    return out


class PgWriteRouteStaticAudit(unittest.TestCase):
    def test_every_pg_write_uses_action_gate_or_is_admin_only(self):
        blocks = _pg_write_route_blocks()
        self.assertGreaterEqual(len(blocks), 20, "expected many /pg write routes")
        for path, src in blocks:
            if path.startswith("/pg/admins"):
                self.assertIn(
                    'require_pg_perm("pg_admins")',
                    src,
                    msg=f"{path} must require mapped pg_admins (Owner-only)",
                )
                continue
            # Users: exact user-action map; others: staff_pg_action
            if path.startswith("/pg/users"):
                self.assertTrue(
                    ("staff_user_actions(" in src)
                    or ("actions[" in src)
                    or ("staff_pg_action(" in src),
                    msg=f"{path} missing user/action ACL gate",
                )
            else:
                self.assertIn(
                    "staff_pg_action(",
                    src,
                    msg=f"{path} missing staff_pg_action gate",
                )
            self.assertIn("_staff_pg(", src, msg=f"{path} must use _staff_pg for credentials")

    def test_staff_pg_write_bodies_do_not_call_get_pg(self):
        """Staff mutation bodies must not fetch owner credentials via get_pg()."""
        blocks = _pg_write_route_blocks()
        for path, src in blocks:
            if path.startswith("/pg/admins"):
                continue
            body_lines = []
            started = False
            for line in src.splitlines():
                if not started:
                    if line.lstrip().startswith("def ") or line.lstrip().startswith("async def "):
                        started = True
                    continue
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                body_lines.append(line)
            body = "\n".join(body_lines)
            self.assertNotIn(
                "get_pg(",
                body,
                msg=f"{path} calls get_pg() in staff write path — owner token leak risk",
            )

    def test_staff_pg_helper_owner_token_only_for_platform_admin(self):
        fn = PG_PAGES[
            PG_PAGES.find("async def _staff_pg") : PG_PAGES.find("async def _assert_owned_user")
        ]
        self.assertIn("return get_pg(), bool(staff.get(\"pg_is_owner\"))", fn)
        self.assertEqual(fn.count("return get_pg(), True"), 0)
        self.assertIn("is_platform_admin(staff)", fn)
        self.assertIn("get_pg_for_reseller", fn)
        self.assertIn("get_pg_for_staff", fn)
        self.assertIn("بدون اعتبارنامه اختصاصی", fn)

    def test_plans_also_create_template_uses_staff_credentials(self):
        self.assertIn("staff_can_create_pg_template", PLAN_PAGES_PY)
        idx = PLAN_PAGES_PY.find("if also_create_template:")
        self.assertGreater(idx, 0)
        chunk = PLAN_PAGES_PY[idx : idx + 2500]
        self.assertIn("_staff_pg(", chunk)
        self.assertIn("create_user_template", chunk)
        self.assertNotIn("get_pg().create_user_template", chunk)
        self.assertNotIn("await get_pg().create_user_template(", chunk)


class RoleMatrixAuthz(unittest.TestCase):
    def test_owner_never_gets_admin_row_actions_in_template(self):
        html = (ROOT / "app" / "web" / "templates" / "pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("{% if src != 'owner' %}", html)
        owner_block = html.split("{% elif src == 'owner' %}", 1)[1].split(
            "{% elif src == 'pg_staff' %}", 1
        )[0]
        self.assertNotIn("row_actions", owner_block)
        # Actions column only rendered when not owner
        self.assertIn("{% call row_actions() %}", html.split("{% if src != 'owner' %}", 1)[1][:400])

    def test_admin_bypasses_staff_pg_action(self):
        from app.services.pg_access import (
            full_pg_owner_features,
            map_pg_role_actions,
            role_user_actions,
        )

        admin = {
            "role": "admin",
            "pg_permissions": full_pg_owner_features(),
            "pg_actions": map_pg_role_actions({"is_owner": True}),
            "pg_user_actions": role_user_actions({"is_owner": True}),
            "pg_writes": {"templates": True},
            "pg_is_owner": True,
        }
        self.assertTrue(staff_pg_action(admin, "hosts", "create"))
        self.assertTrue(staff_pg_action(admin, "users", "delete"))
        self.assertTrue(staff_can_create_pg_template(admin))
        self.assertTrue(staff_user_actions(admin)["create"])
        # Hybrid fail-closed without enrichment
        self.assertFalse(staff_pg_action({"role": "admin"}, "hosts", "create"))

    def test_reseller_limited_permissions_denied(self):
        reseller = {
            "role": "reseller",
            "bot_user_id": 9,
            "pg_writes": {"hosts": False, "templates": True, "users": True},
            "pg_actions": {
                "hosts": {"create": False, "update": False, "delete": False},
                "templates": {"create": False, "update": True, "delete": False},
                "users": {"create": True, "update": False, "delete": False},
            },
            "pg_user_actions": {
                "create": True,
                "update": False,
                "delete": False,
                "reset_usage": False,
                "revoke_sub": False,
                "disable": False,
                "enable": False,
            },
        }
        self.assertFalse(staff_pg_action(reseller, "hosts", "create"))
        self.assertFalse(staff_can_create_pg_template(reseller))
        self.assertTrue(staff_pg_action(reseller, "templates", "update"))
        ua = staff_user_actions(reseller)
        self.assertTrue(ua["create"])
        self.assertFalse(ua["update"])
        self.assertFalse(ua["delete"])

    def test_pg_staff_limited_permissions_denied(self):
        staff = {
            "role": "pg_staff",
            "pg_admin_username": "s1",
            "pg_writes": {"groups": True},
            "pg_actions": {
                "groups": {"create": False, "update": True, "delete": False},
                "hosts": {"create": False, "update": False, "delete": False},
            },
            "pg_user_actions": {"create": False, "update": False, "delete": False},
        }
        self.assertFalse(staff_pg_action(staff, "groups", "create"))
        self.assertTrue(staff_pg_action(staff, "groups", "update"))
        self.assertFalse(staff_pg_action(staff, "hosts", "delete"))
        self.assertFalse(staff_user_actions(staff)["create"])

    def test_ui_create_flags_require_matching_server_helpers(self):
        for flag, needle in [
            ("can_create=", 'staff_pg_action(staff, "hosts", "create")'),
            ("can_update=", 'staff_pg_action(staff, "hosts", "update")'),
            ("can_create=", 'staff_pg_action(staff, "templates", "create")'),
            ("can_update=", 'staff_pg_action(staff, "templates", "update")'),
            ("can_delete=", 'staff_pg_action(staff, "templates", "delete")'),
            ("can_create=", 'staff_pg_action(staff, "groups", "create")'),
            ("can_update=", 'staff_pg_action(staff, "groups", "update")'),
            ("can_delete=", 'staff_pg_action(staff, "groups", "delete")'),
        ]:
            self.assertIn(needle, PG_PAGES, msg=f"missing server gate for {needle}")
            # flag name appears in render kwargs near the gate (same file)
            self.assertIn(flag, PG_PAGES)


class StaffPgCredentialMatrix(unittest.IsolatedAsyncioTestCase):
    async def test_admin_uses_owner_client(self):
        from app.api.pg_pages import _staff_pg

        fake = MagicMock()
        with patch("app.api.pg_pages.get_pg", return_value=fake):
            client, as_owner = await _staff_pg(
                AsyncMock(),
                {
                    "role": "admin",
                    "web_owner": True,
                    "pg_is_owner": True,
                    "org_principal_id": 1,
                    "org_depth": 0,
                    "org_parent_id": None,
                    "org_status": "active",
                },
            )
        self.assertTrue(as_owner)
        self.assertIs(client, fake)

    async def test_reseller_never_gets_owner_client(self):
        from app.api.pg_pages import _staff_pg

        fake = MagicMock()
        with (
            patch("app.api.pg_pages.get_pg") as gp,
            patch(
                "app.api.pg_pages.get_pg_for_reseller",
                new=AsyncMock(return_value=fake),
            ),
        ):
            client, as_owner = await _staff_pg(
                AsyncMock(),
                {"role": "reseller", "bot_user_id": 7, "pg_admin_username": "r"},
            )
        self.assertFalse(as_owner)
        self.assertIs(client, fake)
        gp.assert_not_called()

    async def test_pg_staff_never_gets_owner_client(self):
        from app.api.pg_pages import _staff_pg

        fake = MagicMock()
        with (
            patch("app.api.pg_pages.get_pg") as gp,
            patch(
                "app.services.pasarguard.get_pg_for_staff",
                new=AsyncMock(return_value=fake),
            ),
        ):
            client, as_owner = await _staff_pg(
                AsyncMock(),
                {"role": "pg_staff", "pg_admin_username": "s1", "pg_staff_id": 2},
            )
        self.assertFalse(as_owner)
        self.assertIs(client, fake)
        gp.assert_not_called()

    async def test_pg_staff_fail_closed_without_credentials(self):
        from app.api.pg_pages import _staff_pg

        with (
            patch("app.api.pg_pages.get_pg") as gp,
            patch(
                "app.services.pasarguard.get_pg_for_staff",
                new=AsyncMock(side_effect=PasarGuardError("رمز ذخیره نشده")),
            ),
        ):
            with self.assertRaises(PasarGuardError):
                await _staff_pg(
                    AsyncMock(),
                    {"role": "pg_staff", "pg_admin_username": "s1"},
                )
        gp.assert_not_called()


class RtlPayloadSafety(unittest.TestCase):
    def test_panel_js_digit_normalize_targets_number_inputs_only(self):
        self.assertIn('input[type="number"]', PANEL_JS)
        self.assertIn("normalizeNumberText", PANEL_JS)
        m = re.search(r"function unlockNumberInputs\([\s\S]*?\n      \}", PANEL_JS)
        self.assertIsNotNone(m)
        block = m.group(0)
        self.assertIn('input[type="number"]', block)
        self.assertNotIn('input[type="hidden"]', block)
        self.assertIn("function isNumericField", PANEL_JS)
        self.assertIn("function applyNormalize", PANEL_JS)

    def test_table_search_contract_unchanged(self):
        """Users search remains a GET form field — RTL must not rename params."""
        users = (ROOT / "app" / "web" / "templates" / "pg_users.html").read_text(encoding="utf-8")
        self.assertIn('name="q"', users)
        self.assertIn('method="get"', users.lower())
        self.assertIn('action="/pg/users"', users)
        self.assertIn('input[dir="ltr"]::placeholder', PANEL_CSS)
        unlock = PANEL_JS[
            PANEL_JS.find("function unlockNumberInputs") : PANEL_JS.find("function applyNormalize")
        ]
        self.assertIn('input[type="number"]', unlock)
        self.assertNotIn('name="q"', unlock)

    def test_hosts_edit_form_preserves_post_action_contract(self):
        html = (ROOT / "app" / "web" / "templates" / "pg_hosts.html").read_text(encoding="utf-8")
        self.assertIn('action="/pg/hosts/0/edit"', html)
        self.assertIn("form.action = '/pg/hosts/' + hid + '/edit'", html)
        self.assertIn('name="remark"', html)
        self.assertIn('name="address"', html)
        self.assertIn('name="inbound_tag"', html)

    def test_templates_edit_form_preserves_post_action_contract(self):
        html = (ROOT / "app" / "web" / "templates" / "pg_templates.html").read_text(encoding="utf-8")
        self.assertIn('action="/pg/templates/0/edit"', html)
        self.assertIn("form.action = '/pg/templates/' + tid + '/edit'", html)
        self.assertIn('name="name"', html)
        self.assertIn('name="data_limit_gb"', html)
        self.assertIn('name="expire_days"', html)

    def test_groups_edit_form_preserves_post_action_contract(self):
        html = (ROOT / "app" / "web" / "templates" / "pg_groups.html").read_text(encoding="utf-8")
        self.assertIn('action="/pg/groups/0/edit"', html)
        self.assertIn("form.action = '/pg/groups/' + gid + '/edit'", html)
        self.assertIn('name="name"', html)

    def test_number_parser_accepts_persian_digits_same_int(self):
        from app.services.numbers import normalize_digits, parse_int

        self.assertEqual(normalize_digits("۱۲۳"), "123")
        self.assertEqual(parse_int("۱۲۳"), 123)
        self.assertEqual(parse_int("123"), 123)

    def test_user_create_payload_builder_unchanged_by_rtl(self):
        from app.services.pasarguard import build_user_create_payload

        payload = build_user_create_payload(
            username="alice_1",
            group_ids=[1, 2],
            data_limit=1024,
            expire_ts=1700000000,
            hwid_limit=3,
            note="web panel",
        )
        self.assertEqual(payload["username"], "alice_1")
        self.assertEqual(payload["group_ids"], [1, 2])
        self.assertEqual(payload["data_limit"], 1024)
        self.assertIn("expire", payload)
        self.assertEqual(payload["hwid_limit"], 3)
        self.assertEqual(payload["note"], "web panel")


class NoUiOnlyAuthzTemplates(unittest.TestCase):
    def test_mutation_forms_gated_by_server_flags(self):
        hosts = (ROOT / "app" / "web" / "templates" / "pg_hosts.html").read_text(encoding="utf-8")
        self.assertIn("can_create", hosts)
        self.assertIn("can_update", hosts)
        self.assertIn("can_delete", hosts)
        tpls = (ROOT / "app" / "web" / "templates" / "pg_templates.html").read_text(encoding="utf-8")
        self.assertIn("can_create", tpls)
        self.assertIn("can_update", tpls)
        groups = (ROOT / "app" / "web" / "templates" / "pg_groups.html").read_text(encoding="utf-8")
        self.assertIn("can_create", groups)
        self.assertIn("can_update", groups)

    def test_server_routes_recheck_same_actions_as_ui_flags(self):
        """UI flags alone are insufficient — POST handlers must re-check."""
        self.assertIn(
            'if not staff_pg_action(staff, "hosts", "create")',
            PG_PAGES,
        )
        self.assertIn(
            'if not staff_pg_action(staff, "hosts", "update")',
            PG_PAGES,
        )
        self.assertIn(
            'if not staff_pg_action(staff, "templates", "update")',
            PG_PAGES,
        )
        self.assertIn(
            'if not staff_pg_action(staff, "groups", "update")',
            PG_PAGES,
        )
        self.assertIn('if not actions["create"]', PG_PAGES)


if __name__ == "__main__":
    unittest.main()
