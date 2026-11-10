"""Web panel navigation registry: items per role, active item, real routes, Mini App shortcuts."""

from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import create_api_app, templates
from app.services.guide_catalog import TOPICS
from app.services.marketing_nav import MarketingSection
from app.services.panel_nav import (
    MINIAPP_OWNER_SHORTCUTS,
    MINIAPP_RESELLER_SHORTCUTS,
    NavViewer,
    build_sidebar,
    miniapp_shortcuts,
)

ALL_PG = frozenset({"pg_overview", "pg_users", "pg_admins", "pg_nodes", "pg_groups", "pg_hosts", "pg_templates", "pg_inbounds"})


def viewer(role: str = "admin", *, perms: tuple[str, ...] = (), pg: frozenset[str] = frozenset(), owner: bool = False,
           add_rep: bool = False, manage_rep: bool = False, marketing: tuple[str, ...] = ()) -> NavViewer:
    return NavViewer(role=role, perms=frozenset(perms), pg_perms=pg, is_owner=owner, can_add_representative=add_rep,
                     can_manage_representatives=manage_rep, marketing_hrefs=marketing)


OWNER = viewer(pg=ALL_PG, owner=True, manage_rep=True, marketing=("/loyalty?tab=overview", "/broadcast"))
RESELLER = viewer("reseller", perms=("dashboard", "orders", "plans", "tickets", "shop_settings"), add_rep=True,
                  marketing=("/catalog-discounts",))
PG_STAFF = viewer("pg_staff", pg=frozenset({"pg_users", "pg_hosts"}))


def labels(v: NavViewer, path: str = "/home", tab: str = "") -> dict[str, list[str]]:
    return {s.key: [i.label for i in s.items] for s in build_sidebar(v, path=path, tab=tab)}


def active(v: NavViewer, path: str, tab: str = "") -> list[str]:
    return [i.label for s in build_sidebar(v, path=path, tab=tab) for i in s.items if i.active]


class SidebarPerRoleTests(unittest.TestCase):
    def test_owner_sees_system_shop_and_pasarguard_sections_in_order(self) -> None:
        nav = labels(OWNER)
        self.assertEqual(nav["system"], ["داشبورد", "مرکز اقدام", "تنظیمات سیستم"])
        self.assertEqual(nav["shop"], ["نمای کلی فروشگاه", "کاربران ربات", "نمایندگان", "مالی", "پلن‌ها", "پشتیبانی",
                                       "بازاریابی", "گزارش کارکنان", "تنظیمات ربات"])
        self.assertEqual(nav["pg"][0], "نمای کلی پاسارگارد")
        self.assertEqual(nav["pg"][-1], "تست اتصال سرویس")

    def test_reseller_gets_an_account_section_and_only_shop_items(self) -> None:
        sections = build_sidebar(RESELLER, path="/home")
        self.assertEqual([s.label for s in sections], ["حساب", "فروشگاه"])
        nav = labels(RESELLER)
        self.assertEqual(nav["system"], ["داشبورد", "مرکز اقدام", "حساب و امنیت"])
        self.assertEqual(nav["shop"][:4], ["نمای کلی فروشگاه", "اشتراک من", "مشتریان", "نمایندگان من"])
        self.assertEqual(nav["shop"][-1], "تنظیمات فروشگاه")
        for owner_only in ("تنظیمات سیستم", "کاربران ربات", "نمایندگان", "تنظیمات ربات", "تست اتصال سرویس"):
            self.assertNotIn(owner_only, nav["system"] + nav["shop"])

    def test_missing_permission_hides_the_item(self) -> None:
        nav = labels(viewer("reseller", perms=("dashboard",)))
        for hidden in ("مالی", "پلن‌ها", "پشتیبانی", "تنظیمات فروشگاه", "نمایندگان من", "بازاریابی"):
            self.assertNotIn(hidden, nav["shop"])

    def test_pasarguard_staff_sees_only_granted_pages(self) -> None:
        nav = labels(PG_STAFF)
        self.assertNotIn("shop", nav)
        self.assertEqual(nav["system"], ["داشبورد", "مرکز اقدام", "حساب و امنیت", "پشتیبانی"])
        self.assertEqual(nav["pg"], ["کاربران", "هاست‌ها"])

    def test_no_item_appears_twice_for_any_role(self) -> None:
        for v in (OWNER, RESELLER, PG_STAFF):
            items = [i.label for s in build_sidebar(v, path="/home") for i in s.items]
            self.assertEqual(len(items), len(set(items)), items)

    def test_marketing_entry_opens_the_first_allowed_section(self) -> None:
        item = next(i for s in build_sidebar(RESELLER, path="/home") for i in s.items if i.key == "marketing")
        self.assertEqual(item.href, "/catalog-discounts")


class ActiveItemTests(unittest.TestCase):
    def test_bot_connection_tab_is_part_of_system_settings(self) -> None:
        self.assertEqual(active(OWNER, "/settings", "bot"), ["تنظیمات سیستم"])

    def test_bot_and_system_settings_tabs_light_their_own_entry(self) -> None:
        self.assertEqual(active(OWNER, "/settings", "messages"), ["تنظیمات ربات"])
        self.assertEqual(active(OWNER, "/settings", "backup"), ["تنظیمات سیستم"])
        self.assertEqual(active(OWNER, "/security"), ["تنظیمات سیستم"])
        self.assertEqual(active(OWNER, "/message-variables"), ["تنظیمات ربات"])

    def test_marketing_pages_light_the_marketing_entry(self) -> None:
        self.assertEqual(active(OWNER, "/broadcast"), ["بازاریابی"])

    def test_finance_subpages_light_finance(self) -> None:
        self.assertEqual(active(OWNER, "/service-cancellations"), ["مالی"])


def _get_routes() -> set[str]:
    app = create_api_app()
    return {r.path for r in app.routes if "GET" in (getattr(r, "methods", None) or set())}


class RealRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.routes = _get_routes()

    def test_every_sidebar_link_has_a_route(self) -> None:
        for v in (OWNER, RESELLER, PG_STAFF):
            for section in build_sidebar(v, path="/home"):
                for item in section.items:
                    self.assertIn(item.href.split("?")[0], self.routes, item.label)

    def test_every_guide_topic_opens_a_real_panel_page(self) -> None:
        for slug, topic in TOPICS.items():
            path = str(topic.get("panel") or "").split("?")[0].rstrip("/") or "/"
            self.assertIn(path, self.routes, slug)


class MiniAppShortcutTests(unittest.TestCase):
    def test_shortcuts_use_the_sidebar_labels_and_keep_their_ids(self) -> None:
        owner = miniapp_shortcuts(MINIAPP_OWNER_SHORTCUTS)
        self.assertEqual([s["id"] for s in owner], ["resellers", "finance", "users", "tickets", "dashboard"])
        self.assertEqual([s["label"] for s in owner], ["نمایندگان", "مالی", "کاربران ربات", "پشتیبانی", "نمای کلی فروشگاه"])
        shop = miniapp_shortcuts(MINIAPP_RESELLER_SHORTCUTS)
        self.assertEqual([(s["id"], s["path"]) for s in shop],
                         [("home", "/home"), ("finance", "/finance"), ("tickets", "/tickets"), ("shop", "/shop-settings")])


class RenderedSidebarTests(unittest.TestCase):
    def render(self, staff: dict, identity: dict, *, is_owner: bool, path: str = "/home", **ctx) -> str:
        request = SimpleNamespace(query_params={}, url=SimpleNamespace(path=path), state=SimpleNamespace())
        return templates.env.get_template("base.html").render(
            request=request, app_version="t", staff=staff, identity=identity, is_owner=is_owner, **ctx)

    def test_unread_dot_and_active_item_are_rendered(self) -> None:
        html = self.render({"role": "admin", "permissions": []}, {"kind": "owner"}, is_owner=True, path="/inbox",
                           inbox_alert=True, marketing_sections=(MarketingSection("club", "باشگاه", "/loyalty"),))
        link = re.search(r'<a href="/inbox" class="nav-item[^"]*"[^>]*>.*?</a>', html, re.S).group(0)
        self.assertIn("active", link)
        self.assertIn('aria-current="page"', link)
        self.assertIn('aria-label="اعلان خوانده‌نشده"', link)
        self.assertIn('href="/loyalty"', html)

    def test_sections_start_collapsed_and_keep_their_keys(self) -> None:
        html = self.render({"role": "admin", "permissions": [], "pg_permissions": ["pg_users"]}, {"kind": "owner"},
                           is_owner=True)
        for key in ("system", "shop", "pg"):
            self.assertIn(f'data-nav-collapse="{key}" aria-expanded="false"', html)
            self.assertIn(f'id="nav-panel-{key}"', html)


if __name__ == "__main__":
    unittest.main()
