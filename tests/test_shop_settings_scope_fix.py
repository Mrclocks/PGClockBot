"""Surgical Shop/Settings scope regressions (finance/supports fail-closed + principal shop).

NO redesign — contract tests for read/write scope only.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fn_src(path: Path, fn_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn_name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
        if isinstance(node, ast.FunctionDef) and node.name == "register_shop_settings":
            for child in node.body:
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == fn_name
                ):
                    return ast.get_source_segment(path.read_text(encoding="utf-8"), child) or ""
        if isinstance(node, ast.FunctionDef) and node.name in {
            "register_finance_pages",
            "register_panel_tickets_pages",
        }:
            for child in node.body:
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == fn_name
                ):
                    return ast.get_source_segment(path.read_text(encoding="utf-8"), child) or ""
    return ""


class FinanceSupportsFailClosedTests(unittest.TestCase):
    def test_finance_no_owner_fallback_when_scope_missing(self):
        src = (ROOT / "app/api/finance_pages.py").read_text(encoding="utf-8")
        # Must gate Owner settings on is_platform_admin, not bare role==admin + None rid.
        self.assertIn("is_platform_admin(staff)", src)
        self.assertNotIn(
            'rid = None if staff.get("role") == "admin" else shop_owner_id(staff)',
            src,
        )
        # Scoped path requires rid before get_all_settings
        chunk = src[src.find("if can_finance_settings:") : src.find("fetch_limit = ")]
        self.assertIn("rid = shop_owner_id(staff)", chunk)
        self.assertIn("if rid:", chunk)
        self.assertIn("get_all_settings(session, reseller_id=rid)", chunk)
        # Owner branch uses platform get_all_settings without reseller_id=None trick
        self.assertIn("await get_all_settings(session)", chunk)

    def test_supports_no_owner_fallback_when_scope_missing(self):
        src = (ROOT / "app/api/panel_tickets_pages.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'rid = None if staff.get("role") == "admin" else shop_owner_id(staff)',
            src,
        )
        chunk = src[src.find("if can_manage_supports:") : src.find("return render(request, \"tickets.html\"")]
        self.assertIn("is_platform_admin(staff)", chunk)
        self.assertIn("rid = shop_owner_id(staff)", chunk)
        self.assertIn("if rid:", chunk)
        self.assertIn("get_all_settings(session, reseller_id=rid)", chunk)


class ShopSettingsPrincipalScopeTests(unittest.TestCase):
    def test_page_allows_session_scope_not_reseller_role_only(self):
        src = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        page = src[src.find("async def shop_settings_page") : src.find("async def shop_settings_save")]
        self.assertNotIn('staff.get("role") != "reseller"', page)
        self.assertIn("_is_owner_settings_actor", page)
        self.assertIn("rid = _rid(staff)", page)
        self.assertIn("get_all_settings(session, reseller_id=rid)", page)
        self.assertIn("_deny_scope", page)

    def test_save_paths_use_same_session_scope(self):
        src = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        for name in (
            "shop_settings_save",
            "shop_notifications_save",
            "shop_menu_layout_save",
            "shop_supports_save",
            "shop_supports_delete",
        ):
            start = src.find(f"async def {name}")
            self.assertGreater(start, 0, name)
            # slice until next async def at same indent level approx
            rest = src[start:]
            nxt = rest.find("\n    async def ", 10)
            chunk = rest[: nxt if nxt > 0 else len(rest)]
            self.assertNotIn('staff.get("role") != "reseller"', chunk, name)
            self.assertIn("_rid(staff)", chunk, name)
            self.assertIn("if not rid:", chunk, name)

    def test_helper_uses_shop_owner_id_server_side(self):
        src = (ROOT / "app/api/shop_settings.py").read_text(encoding="utf-8")
        self.assertIn("def _rid(staff: dict)", src)
        self.assertIn("shop_owner_id(staff)", src)
        self.assertIn("def _is_owner_settings_actor", src)
        self.assertIn("is_platform_admin", src)
        # Authority comment / ignore client reseller_id
        self.assertIn("Ignore any client-supplied reseller_id", src)


class ShopOwnerIdPrincipalTests(unittest.TestCase):
    def test_principal_with_profile_resolves_shop_id(self):
        from app.services.shop_scope import shop_owner_id

        self.assertEqual(
            shop_owner_id(
                {
                    "role": "principal",
                    "reseller_profile_id": 9,
                    "bot_user_id": 55,
                }
            ),
            55,
        )

    def test_principal_missing_scope_is_none(self):
        from app.services.shop_scope import shop_owner_id

        self.assertIsNone(
            shop_owner_id({"role": "principal", "reseller_profile_id": 0, "bot_user_id": 55})
        )
        self.assertIsNone(
            shop_owner_id({"role": "principal", "reseller_profile_id": 9, "bot_user_id": 0})
        )

    def test_sibling_ids_differ(self):
        from app.services.shop_scope import shop_owner_id

        a = shop_owner_id(
            {"role": "reseller", "bot_user_id": 10}
        )
        b = shop_owner_id(
            {"role": "principal", "reseller_profile_id": 2, "bot_user_id": 20}
        )
        self.assertEqual(a, 10)
        self.assertEqual(b, 20)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
