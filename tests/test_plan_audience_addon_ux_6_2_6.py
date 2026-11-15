"""v6.2.6 — plan audience + addon packs strip extras; catalog gated."""

from __future__ import annotations

import unittest
from pathlib import Path
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]
PLANS_HTML = template_with_includes("plans.html")
EDIT_HTML = (ROOT / "app/web/templates/reseller_plan_edit.html").read_text(encoding="utf-8")
RESELLER_PAGES = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
KEYBOARDS = (ROOT / "app/bot/reply_keyboards.py").read_text(encoding="utf-8")
RESELLER_BOT = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")


class PlanAudienceAddonUxTests(unittest.TestCase):
    def test_modal_labels_audience_then_kind(self):
        self.assertIn("مخاطب پلن", PLANS_HTML)
        # نوع پلن sits under مخاطب پلن in a stacked form-stack
        aud = PLANS_HTML.find("مخاطب پلن")
        kind = PLANS_HTML.find("نوع پلن", aud)
        self.assertGreater(kind, aud)
        self.assertIn("addon_volume", PLANS_HTML)
        self.assertIn("بسته حجم", PLANS_HTML)
        self.assertIn("بسته کاربر", PLANS_HTML)
        self.assertIn("reseller-subscription-extras", PLANS_HTML)
        self.assertIn("syncResellerForm", PLANS_HTML)
        # No duplicate inner نوع پلن select for plan_kind
        self.assertNotIn('<select name="plan_kind" id="reseller-plan-kind">', PLANS_HTML)
        self.assertIn('id="reseller-plan-kind"', PLANS_HTML)
        self.assertIn('type="hidden"', PLANS_HTML[PLANS_HTML.find("reseller-plan-kind") - 80 :])

    def test_addon_section_in_list(self):
        self.assertIn('id="reseller-plans-addons"', PLANS_HTML)
        self.assertIn("addon_reseller_plans", PLANS_HTML)
        self.assertIn("subscription_reseller_plans", PLANS_HTML)

    def test_edit_page_hides_extras_for_addons(self):
        self.assertIn("مخاطب پلن", EDIT_HTML)
        self.assertIn("edit-subscription-extras", EDIT_HTML)
        self.assertIn("edit-plan-kind", EDIT_HTML)

    def test_create_api_strips_addon_extras(self):
        create = RESELLER_PAGES[
            RESELLER_PAGES.find("async def reseller_plan_create") : RESELLER_PAGES.find(
                "async def reseller_plan_edit_page"
            )
        ]
        self.assertIn("_is_addon_plan_kind", create)
        self.assertIn('create_pg_admin=False', create)
        self.assertIn('share_pg_panel_url=False', create)
        self.assertIn('allow_buy_extra=False', create)
        self.assertIn('pg_role_id=None', create)
        self.assertIn('billing_mode="fixed"', create)

    def test_edit_api_strips_addon_extras(self):
        edit = RESELLER_PAGES[
            RESELLER_PAGES.find("async def reseller_plan_edit_save") : RESELLER_PAGES.find(
                "async def reseller_plan_toggle"
            )
        ]
        self.assertIn("_is_addon_plan_kind", edit)
        self.assertIn("plan.create_pg_admin = False", edit)
        self.assertIn("plan.pg_role_id = None", edit)

    def test_bot_menu_fail_closed_for_addons(self):
        block = KEYBOARDS[
            KEYBOARDS.find("def _reseller_capacity_entries") : KEYBOARDS.find(
                "def _reseller_submenu_entries"
            )
        ]
        self.assertIn("is_subscription_plan", block)
        self.assertIn('entries.append(("res_addon_packs"', block)
        except_tail = block[block.rfind("except Exception:") :]
        self.assertIn("Fail closed", except_tail)
        self.assertNotIn('entries.append(("res_addon_packs"', except_tail)
        submenu = KEYBOARDS[
            KEYBOARDS.find("def _reseller_submenu_entries") : KEYBOARDS.find(
                "def reseller_hub_main_keyboard"
            )
        ]
        self.assertIn("_reseller_capacity_entries", submenu)

    def test_res_addons_requires_subscription_plan(self):
        fn = RESELLER_BOT[
            RESELLER_BOT.find("async def res_addons") : RESELLER_BOT.find(
                "async def res_addon_buy"
            )
        ]
        self.assertIn("is_subscription_plan", fn)
        self.assertIn("اشتراک فعال", fn)
        buy = RESELLER_BOT[RESELLER_BOT.find("async def res_addon_buy") :]
        self.assertIn("is_addon_plan", buy)
        self.assertIn("is_subscription_plan", buy)


if __name__ == "__main__":
    unittest.main()
