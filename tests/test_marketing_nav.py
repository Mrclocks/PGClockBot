"""Marketing & loyalty navigation exposes exactly the sections a role may open."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.catalog_discounts import authorized_scope
from app.api.customer_features import feature_scope
from app.api.loyalty_pages import _shop_scope
from app.services.funnel_access import funnel_shop_scope
from app.services.gift_codes import gift_code_scope
from app.services.authz import authz_from_staff, can_shop
from app.services.marketing_nav import (
    BROADCAST,
    CAMPAIGNS,
    CLUB,
    CLUB_TRANSACTIONS,
    DISCOUNTS,
    FUNNEL,
    GIFT_CODES,
    TRIAL_USERS,
    MarketingSection,
    marketing_sections,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "app/web/templates"

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}
LEGACY_ADMIN = {"role": "admin", "permissions": []}


def _reseller(permissions: list[str], shop_id: int | None = 5) -> dict:
    return {"role": "reseller", "permissions": permissions, "bot_user_id": shop_id}


PROFILES = [
    OWNER,
    LEGACY_ADMIN,
    _reseller(["campaigns"]),
    _reseller(["loyalty"]),
    _reseller(["loyalty", "campaigns"]),
    _reseller(["catalog_discounts"]),
    _reseller(["catalog_discounts"], shop_id=None),
    _reseller(["plans"]),
    _reseller(["payments"]),
    _reseller(["orders"], shop_id=None),
    _reseller(["campaigns"], shop_id=None),
    _reseller([]),
    {"role": "pg_staff", "permissions": ["loyalty", "campaigns"]},
]


def _campaigns_route_allows(staff: dict) -> bool:
    try:
        feature_scope(staff, "campaigns")
    except HTTPException:
        return False
    return True


def _gift_codes_route_allows(staff: dict) -> bool:
    try:
        gift_code_scope(staff)
    except PermissionError:
        return False
    return True


def _funnel_route_allows(staff: dict) -> bool:
    try:
        funnel_shop_scope(staff)
    except PermissionError:
        return False
    return True


def _discounts_route_allows(staff: dict) -> bool:
    try:
        authorized_scope(staff)
    except HTTPException:
        return False
    return True


def _loyalty_route_allows(staff: dict) -> bool:
    """``GET /loyalty``: ``require_perm("loyalty")`` then a resolvable shop scope."""
    if not can_shop(authz_from_staff(staff), "loyalty"):
        return False
    try:
        _shop_scope(staff)
    except ValueError:
        return False
    return True


def _keys(staff: dict | None) -> list[str]:
    return [section.key for section in marketing_sections(staff)]


class MarketingSectionVisibilityTests(unittest.TestCase):
    def test_unauthenticated_sees_nothing(self):
        self.assertEqual(marketing_sections(None), ())
        self.assertEqual(marketing_sections({}), ())

    def test_owner_sees_every_section_in_order(self):
        self.assertEqual(_keys(OWNER), [CLUB, CLUB_TRANSACTIONS, DISCOUNTS, GIFT_CODES, CAMPAIGNS, TRIAL_USERS, FUNNEL, BROADCAST])

    def test_legacy_admin_without_owner_principal_gets_no_tenant_sections(self):
        self.assertEqual(marketing_sections(LEGACY_ADMIN), ())

    def test_reseller_sees_only_the_features_granted_to_them(self):
        self.assertEqual(
            _keys(_reseller(["loyalty", "campaigns"])),
            [CLUB, CLUB_TRANSACTIONS, CAMPAIGNS],
        )
        self.assertEqual(_keys(_reseller(["campaigns"])), [CAMPAIGNS])
        self.assertEqual(_keys(_reseller(["loyalty"])), [CLUB, CLUB_TRANSACTIONS])

    def test_discounts_follow_their_own_permission(self):
        self.assertEqual(_keys(_reseller(["catalog_discounts"])), [DISCOUNTS])
        self.assertNotIn(DISCOUNTS, _keys(_reseller(["loyalty", "campaigns"])))
        self.assertEqual(_keys(_reseller(["catalog_discounts"], shop_id=None)), [])

    def test_funnel_follows_the_orders_or_payments_permission(self):
        self.assertEqual(_keys(_reseller(["payments"])), [FUNNEL])
        self.assertEqual(_keys(_reseller(["orders"])), [FUNNEL])
        self.assertEqual(_keys(_reseller(["orders", "gift_codes"])), [GIFT_CODES, FUNNEL])
        self.assertEqual(_keys(_reseller(["orders"], shop_id=None)), [])
        self.assertNotIn(FUNNEL, _keys(_reseller(["plans", "loyalty"])))

    def test_gift_codes_follow_the_gift_codes_permission(self):
        self.assertEqual(_keys(_reseller(["gift_codes"])), [GIFT_CODES])
        self.assertNotIn(GIFT_CODES, _keys(_reseller(["plans"])))
        self.assertNotIn(GIFT_CODES, _keys(_reseller(["orders"])))
        self.assertEqual(_keys(_reseller(["gift_codes"], shop_id=None)), [])
        self.assertNotIn(GIFT_CODES, _keys(_reseller(["loyalty", "campaigns"])))

    def test_reseller_never_sees_broadcast(self):
        every_perm = ["loyalty", "campaigns", "broadcast", "shop_settings", "dashboard"]
        self.assertNotIn(BROADCAST, _keys(_reseller(every_perm)))

    def test_reseller_without_feature_permission_sees_nothing(self):
        self.assertEqual(_keys(_reseller(["dashboard", "tickets"])), [])

    def test_campaigns_hidden_when_reseller_has_no_shop(self):
        self.assertEqual(_keys(_reseller(["campaigns"], shop_id=None)), [])
        self.assertEqual(_keys(_reseller(["campaigns"], shop_id=0)), [])

    def test_pg_staff_sees_nothing(self):
        staff = {"role": "pg_staff", "permissions": ["loyalty", "campaigns"]}
        self.assertEqual(marketing_sections(staff), ())

    def test_forged_owner_flag_on_a_reseller_grants_nothing(self):
        staff = _reseller([])
        staff["web_owner"] = True
        staff["is_owner"] = True
        self.assertEqual(marketing_sections(staff), ())

    def test_visibility_matches_the_route_guards(self):
        for staff in PROFILES:
            with self.subTest(staff=staff):
                self.assertEqual(CAMPAIGNS in _keys(staff), _campaigns_route_allows(staff))
                self.assertEqual(DISCOUNTS in _keys(staff), _discounts_route_allows(staff))
                self.assertEqual(GIFT_CODES in _keys(staff), _gift_codes_route_allows(staff))
                self.assertEqual(FUNNEL in _keys(staff), _funnel_route_allows(staff))
                self.assertEqual(CLUB in _keys(staff), _loyalty_route_allows(staff))
                self.assertEqual(CLUB_TRANSACTIONS in _keys(staff), _loyalty_route_allows(staff))


def _render_tabs(sections, active: str) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.from_string(
        '{% from "_marketing_tabs.html" import marketing_tabs %}'
        "{{ marketing_tabs(sections, active) }}"
    )
    return template.render(sections=sections, active=active)


class MarketingTabsTemplateTests(unittest.TestCase):
    def test_marks_only_the_active_section(self):
        html = _render_tabs(marketing_sections(OWNER), CAMPAIGNS)
        self.assertEqual(html.count('class="active"'), 1)
        self.assertIn('href="/campaigns" class="active" aria-current="page"', html)
        self.assertIn('href="/broadcast" class=""', html)

    def test_single_section_renders_no_strip(self):
        self.assertEqual(_render_tabs(marketing_sections(_reseller(["campaigns"])), CAMPAIGNS).strip(), "")

    def test_missing_sections_render_no_strip(self):
        self.assertEqual(_render_tabs((), CLUB).strip(), "")

    def test_labels_are_escaped(self):
        hostile = (MarketingSection("x", "<script>alert(1)</script>", "/loyalty"),) * 2
        html = _render_tabs(hostile, "x")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()


def _request(path: str):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"example.test")],
            "client": ("127.0.0.1", 123),
            "server": ("example.test", 443),
            "state": {},
        }
    )


def _page(name: str, path: str, staff: dict, **extra) -> str:
    from app.api.app import render

    ctx = {"staff": staff, "history": [], "audience_labels": {}, **extra}
    return render(_request(path), name, ctx).body.decode("utf-8")


class MarketingNavRenderedPageTests(unittest.TestCase):
    """The real ``render()`` path: sidebar entry and tab strip follow the role."""

    def test_owner_gets_sidebar_entry_and_full_strip_on_broadcast(self):
        html = _page("broadcast.html", "/broadcast", OWNER)
        self.assertIn("بازاریابی و وفاداری", html)
        self.assertIn('href="/loyalty?tab=overview"', html)
        self.assertIn('href="/campaigns"', html)
        self.assertIn('href="/broadcast" class="active" aria-current="page"', html)

    def test_reseller_with_campaigns_only_gets_no_broadcast_or_club_links(self):
        staff = _reseller(["campaigns"])
        html = _page(
            "customer_features.html",
            "/campaigns",
            staff,
            audiences={},
            audience="trial",
            days=7,
            count=0,
            limit=10,
            campaigns=[],
            mode="campaigns",
        )
        self.assertIn('href="/campaigns"', html)
        self.assertNotIn('href="/broadcast"', html)
        self.assertNotIn('href="/loyalty', html)
        self.assertNotIn('aria-label="بازاریابی و وفاداری"', html)

    def test_reseller_without_marketing_permissions_gets_no_entry(self):
        html = _page("broadcast.html", "/broadcast", _reseller(["dashboard"]))
        self.assertNotIn("بازاریابی و وفاداری", html)
        self.assertNotIn('href="/broadcast"', html)
        self.assertNotIn('href="/campaigns"', html)


class MarketingNavDiscountsPageTests(unittest.TestCase):
    def test_discounts_page_keeps_its_sidebar_entry_and_joins_the_strip(self):
        staff = _reseller(["catalog_discounts", "loyalty"])
        html = _page(
            "catalog_discounts.html",
            "/catalog-discounts",
            staff,
            discounts=[],
            plans=[],
            categories=[],
            announcement_status=None,
            deadlines={},
            next_before=None,
            scopes={},
            modes={},
            request_key="k",
        )
        self.assertIn("بازاریابی و وفاداری", html)
        self.assertIn('href="/catalog-discounts" class="active" aria-current="page"', html)
        self.assertNotIn('href="/broadcast"', html)
