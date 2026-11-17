"""v4.10.8 — create reseller plan select + PG admin as_reseller gated fields."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESELLERS_HTML = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
PG_ADMINS_HTML = (ROOT / "app/web/templates/pg_admins.html").read_text(encoding="utf-8")
RESELLER_API = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
PG_API = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
RESELLERS_SVC = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")


class ResellerCreatePlanUiTests(unittest.TestCase):
    def test_create_modal_has_plan_select_not_commission(self):
        start = RESELLERS_HTML.find('id="modal-reseller-create"')
        end = RESELLERS_HTML.find('id="modal-reseller-edit"')
        modal = RESELLERS_HTML[start:end]
        self.assertIn('name="plan_id"', modal)
        self.assertIn("پلن نمایندگی", modal)
        self.assertIn("— انتخاب پلن —", modal)
        self.assertIn("[PAYG]", modal)
        self.assertIn("[ثابت]", modal)
        self.assertIn("reseller_plans", modal)
        self.assertNotIn('name="commission_percent"', modal)
        self.assertNotIn("کمیسیون ٪", modal)

    def test_plan_select_matches_pg_admins_pattern(self):
        start = RESELLERS_HTML.find('id="modal-reseller-create"')
        end = RESELLERS_HTML.find('id="modal-reseller-edit"')
        modal = RESELLERS_HTML[start:end]
        self.assertIn("required", modal)
        self.assertIn("disabled", modal)
        self.assertIn("هنوز پلن نمایندگی فعالی ندارید", modal)


class ResellerCreatePlanApiTests(unittest.TestCase):
    def test_list_passes_active_plans(self):
        self.assertIn("list_active_reseller_plans", RESELLER_API)
        self.assertIn('"reseller_plans": reseller_plans', RESELLER_API)

    def test_create_requires_active_plan(self):
        fn = RESELLER_API[
            RESELLER_API.find("async def reseller_create") : RESELLER_API.find(
                "async def reseller_edit_page"
            )
        ]
        self.assertIn('form.get("plan_id")', fn)
        self.assertIn("انتخاب پلن نمایندگی الزامی است", fn)
        self.assertIn("پلن نمایندگی نامعتبر یا غیرفعال است", fn)
        self.assertIn("plan=plan", fn)
        self.assertNotIn("commission_percent", fn)
        self.assertNotIn('form.get("commission_percent")', fn)


class PgAdminCreateResellerGateTests(unittest.TestCase):
    def _create_modal(self) -> str:
        start = PG_ADMINS_HTML.find('id="modal-pg-admin-create"')
        end = PG_ADMINS_HTML.find("function syncCreateAdminMode")
        return PG_ADMINS_HTML[start:end]

    def test_as_reseller_default_off_and_fields_hidden(self):
        modal = self._create_modal()
        self.assertIn("create-as-reseller", modal)
        self.assertNotIn("checked=true, help='ادمین پاسارگارد", modal)
        self.assertIn("checked=false", modal)
        self.assertIn('id="create-reseller-fields"', modal)
        self.assertIn("hidden", modal.split('id="create-reseller-fields"', 1)[1][:80])
        # staff web wrap visible by default (no hidden on the wrap itself)
        staff = modal.split('id="create-staff-web-wrap"', 1)[1][:40]
        self.assertNotIn("hidden", staff)

    def test_reseller_fields_include_plan_and_panel_perms(self):
        modal = self._create_modal()
        block = modal.split('id="create-reseller-fields"', 1)[1]
        self.assertIn('name="plan_id"', block)
        self.assertIn("سطح دسترسی پنل ربات", block)
        self.assertIn("feature_perms", block)
        self.assertIn("perm_", block)
        self.assertIn("share_pg_panel_url", block)

    def test_css_hides_gated_reseller_fields(self):
        self.assertIn("#create-reseller-fields[hidden]", CSS)
        self.assertIn("create-reseller-fields[hidden]", CSS)
        self.assertIn("display: none !important", CSS)

    def test_page_passes_feature_perms(self):
        self.assertIn("feature_perms=FEATURE_PERMS", PG_API)

    def test_create_admin_applies_perm_overrides(self):
        fn = PG_API[
            PG_API.find("async def pg_admins_create") : PG_API.find(
                "async def pg_admins_web_access_legacy"
            )
        ]
        self.assertIn("reseller_perms", fn)
        self.assertIn("web_permissions=reseller_perms", fn)
        self.assertIn("share_pg_panel_url=share_pg", fn)
        self.assertIn("default UI is OFF", fn)

    def test_provision_accepts_permission_override(self):
        fn = RESELLERS_SVC[
            RESELLERS_SVC.find("async def provision_existing_pg_admin") : RESELLERS_SVC.find(
                "async def convert_staff_to_reseller"
            )
        ]
        self.assertIn("web_permissions: str | None = None", fn)
        self.assertIn("share_pg_panel_url: bool | None = None", fn)
        self.assertIn("web_permissions or plan.web_permissions", fn)


if __name__ == "__main__":
    unittest.main()
