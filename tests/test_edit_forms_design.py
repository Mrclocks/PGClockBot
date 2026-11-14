"""Edit pages use the plan modal's sections, «نامحدود» toggles and grouped permissions, posting the same fields."""

from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import ChainableUndefined

from app.api.app import templates
from app.services.resellers import FEATURE_PERMS

PLAN_EDIT_FIELDS = {
    "name", "price", "description", "duration_days", "data_limit_gb", "carry_remaining_quota", "mode", "group_1",
    "pg_template_id", "pg_username_prefix", "pg_username_suffix", "pg_username_pattern", "allow_buy_extra",
    "extra_gb_price", "extra_month_price", "plan_vol_amount", "plan_dur_amount", "category_id", "sort_order", "button_style",
}
RESELLER_PLAN_EDIT_FIELDS = {
    "plan_kind", "name", "price", "billing_mode", "price_per_gb", "duration_days", "included_gb", "included_users",
    "addon_gb", "addon_users", "renew_price", "renew_pricing_mode", "carry_remaining_period", "keep_extras_on_renew",
    "allow_buy_extra", "extra_gb_price", "extra_user_price", "extra_gb_amount", "extra_user_amount", "group_1",
    "pg_role_id", "create_pg_admin", "share_pg_panel_url", "category_id", "description", "sort_order", "button_style",
    "is_active",
}
SELECTED = {"dashboard", "orders", "wallet_adjust"}


def plan(**over: object) -> SimpleNamespace:
    base = dict(
        id=7, name="P", price=1000, duration_days=0, data_limit_gb=None, description="", category_id=None, sort_order=1,
        pg_template_id=None, pg_group_ids="1", pg_username_prefix="", pg_username_suffix="", pg_username_pattern="",
        allow_buy_extra=False, extra_gb_price=0, extra_month_price=0, button_style=None, carry_remaining_quota=True,
        plan_kind="subscription", is_system=False, billing_mode="fixed", price_per_gb=0, included_gb=0, included_users=50,
        addon_gb=0, addon_users=0, renew_price=0, renew_pricing_mode="fixed", carry_remaining_period=True,
        keep_extras_on_renew=False, extra_user_price=0, create_pg_admin=True, share_pg_panel_url=False, pg_role_id=1,
        is_active=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def render(name: str, **ctx: object) -> str:
    env = templates.env.overlay(undefined=ChainableUndefined)
    request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/edit"), cookies={})
    base = {"request": request, "staff": {"role": "admin"}, "path": "/edit", "groups": [{"id": 1, "name": "G"}],
            "templates": [], "plan_categories": [], "pg_limit_cards": [], "values": {}, "pg_roles": [{"id": 1, "name": "r"}],
            "plan_style_options": [("inherit", "ارث", "default")], "pack_style_options": [("inherit", "ارث", "default")],
            "feature_perms": FEATURE_PERMS, "selected_perms": SELECTED, "plan_group_ids": ["1"]}
    base.update(ctx)
    return env.get_template(name).render(**base)


def names(html: str) -> set[str]:
    return set(re.findall(r'\bname="([a-zA-Z_0-9]+)"', html))


def checked_perms(html: str) -> set[str]:
    return set(re.findall(r'name="perm_([a-z_]+)" value="1"[^>]*\bchecked\b', html))


class PlanEditFormTests(unittest.TestCase):
    def test_posts_every_field_the_edit_route_reads(self) -> None:
        self.assertLessEqual(PLAN_EDIT_FIELDS, names(render("plan_edit.html", plan=plan())))

    def test_empty_duration_and_volume_show_as_unlimited_toggles(self) -> None:
        html = render("plan_edit.html", plan=plan())
        self.assertIn('data-ds-unlimited="#edit-plan-days" data-ds-empty-is-unlimited', html)
        self.assertIn('data-ds-unlimited="#edit-plan-gb" data-ds-empty-is-unlimited', html)
        self.assertNotIn("خالی = نامحدود", html)

    def test_sections_follow_the_create_form(self) -> None:
        html = render("plan_edit.html", plan=plan())
        titles = re.findall(r'<h3 class="ds-sec-title">([^<]+)</h3>', html)
        self.assertEqual(titles, ["مشخصات", "مدت و حجم", "اتصال پاسارگارد", "فروش حجم و زمان اضافه", "نمایش در ربات"])

    def test_plan_name_is_escaped(self) -> None:
        html = render("plan_edit.html", plan=plan(name="<b>x</b>"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn('value="<b>x</b>"', html)


class ResellerPlanEditFormTests(unittest.TestCase):
    def test_posts_every_field_the_edit_route_reads(self) -> None:
        html = render("reseller_plan_edit.html", plan=plan())
        self.assertLessEqual(RESELLER_PLAN_EDIT_FIELDS, names(html))
        self.assertEqual({n for n in names(html) if n.startswith("perm_")}, {f"perm_{k}" for k, _ in FEATURE_PERMS})

    def test_permissions_are_grouped_and_keep_the_saved_choice(self) -> None:
        html = render("reseller_plan_edit.html", plan=plan())
        self.assertEqual(checked_perms(html), SELECTED)
        self.assertIn("data-ds-perms", html)

    def test_zero_capacity_and_duration_use_unlimited_toggles(self) -> None:
        html = render("reseller_plan_edit.html", plan=plan())
        for field_id in ("edit-duration-days", "edit-included-gb", "edit-included-users"):
            self.assertIn(f'data-ds-unlimited="#{field_id}"', html)
        self.assertNotIn("۰ = زمان نامحدود", html)

    def test_subscription_only_sections_are_marked_for_the_kind_switch(self) -> None:
        html = render("reseller_plan_edit.html", plan=plan())
        self.assertEqual(len(re.findall(r"<section[^>]*data-edit-subscription-only", html)), 3)


class ResellerEditBodyTests(unittest.TestCase):
    def test_permissions_are_grouped_and_keep_the_saved_choice(self) -> None:
        html = render("_reseller_edit_body.html", profile=SimpleNamespace(is_active=True, share_pg_panel_url=False))
        self.assertEqual(checked_perms(html), SELECTED)
        self.assertEqual({n for n in names(html) if n.startswith("perm_")}, {f"perm_{k}" for k, _ in FEATURE_PERMS})


if __name__ == "__main__":
    unittest.main()
