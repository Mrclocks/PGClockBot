"""The plans page shows user plans and reseller plans in two tabs under the page header."""
import re
import unittest
from types import SimpleNamespace as NS

from app.api.app import templates


def render(*, admin: bool, tab: str | None = None) -> str:
    request = NS(url=NS(path="/plans"), query_params={"tab": tab} if tab else {}, state=NS(), cookies={}, headers={})
    return templates.env.get_template("plans.html").render(
        request=request, staff={"role": "admin"}, is_platform_admin=admin, values={}, plans=[], reseller_plans=[],
        csp_nonce="n", csrf_token="T", pg_limit_cards=[], service_addon_packs=[],
    )


def panel_tag(html: str, name: str) -> str | None:
    match = re.search(rf'<div data-plans-panel="{name}"[^>]*>', html)
    return match.group(0) if match else None


class PlansPageTabsTests(unittest.TestCase):
    def test_owner_sees_two_tabs_right_after_the_header_with_users_first(self):
        html = render(admin=True)
        self.assertLess(html.index('class="page-head"'), html.index("data-plans-tabs"))
        self.assertLess(html.index("data-plans-tabs"), html.index('data-plans-panel="users"'))
        self.assertLess(html.index("پلن کاربر"), html.index("پلن نماینده"))
        self.assertIn("hidden", panel_tag(html, "resellers"))
        self.assertNotIn("hidden", panel_tag(html, "users"))
        self.assertIn('href="/plans?tab=resellers"', html)

    def test_the_resellers_tab_is_chosen_by_the_link_without_javascript(self):
        html = render(admin=True, tab="resellers")
        self.assertIn("hidden", panel_tag(html, "users"))
        self.assertNotIn("hidden", panel_tag(html, "resellers"))
        active = re.findall(r'data-plans-tab="(\w+)" class="active"', html)
        self.assertEqual(active, ["resellers"])

    def test_each_kind_of_plan_lives_only_in_its_own_panel(self):
        html = render(admin=True)
        resellers_panel = html.index('data-plans-panel="resellers"')
        self.assertLess(html.index("پلن‌های کاربران"), resellers_panel)
        self.assertGreater(html.index('id="reseller-plans"'), resellers_panel)
        self.assertEqual(html.count("<div"), html.count("</div>"))

    def test_a_shop_without_reseller_plans_gets_no_tabs_and_no_reseller_panel(self):
        for tab in (None, "resellers"):
            html = render(admin=False, tab=tab)
            self.assertNotIn("data-plans-tabs", html)
            self.assertIsNone(panel_tag(html, "resellers"))
            self.assertNotIn("hidden", panel_tag(html, "users"))
            self.assertNotIn('id="reseller-plans"', html)

    def test_the_tab_script_is_loaded_with_the_csp_nonce(self):
        self.assertIn('plans-tabs.js?v=', render(admin=True))
        self.assertRegex(render(admin=True), r'<script src="/static/plans-tabs\.js[^"]*" nonce="n" defer></script>')


if __name__ == "__main__":
    unittest.main()
