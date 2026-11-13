"""Web panel navigation registry: items per role, active item, real routes, Mini App shortcuts,
the phone bottom bar and the dashboard shortcuts."""

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
    DASHBOARD_SHORTCUT_LIMIT,
    DOCK_SLOTS,
    MINIAPP_OWNER_SHORTCUTS,
    MINIAPP_RESELLER_SHORTCUTS,
    NavViewer,
    build_dock,
    build_shortcuts,
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
                                       "بازاریابی", "پاکسازی سرویس‌ها", "گزارش کارکنان", "تنظیمات ربات"])
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

    def test_service_cleanup_sidebar_item_has_an_icon(self) -> None:
        html = self.render({"role": "admin", "permissions": []}, {"kind": "owner"}, is_owner=True)
        link = re.search(r'<a href="/service-cleanup" class="nav-item[^"]*"[^>]*>.*?</a>', html, re.S)
        self.assertIsNotNone(link)
        self.assertRegex(link.group(0), r'<svg class="nav-ico"[^>]*>\s*<path ')


def dock_keys(v: NavViewer, path: str = "/home") -> list[str]:
    return [i.key for i in build_dock(build_sidebar(v, path=path)).items]


def shortcut_rows(v: NavViewer) -> list[tuple[str, str, bool]]:
    return [(sc.item.label, sc.section_label, sc.in_dock) for sc in build_shortcuts(build_sidebar(v, path="/home"))]


class BottomBarTests(unittest.TestCase):
    def test_owner_bar_holds_dashboard_action_center_finance_and_users(self) -> None:
        dock = build_dock(build_sidebar(OWNER, path="/home"))
        self.assertEqual([i.key for i in dock.items], ["home", "inbox", "finance", "users"])
        self.assertEqual([i.short_label for i in dock.items], ["داشبورد", "مرکز اقدام", "مالی", "کاربران"])

    def test_reseller_bar_uses_customers_and_skips_pages_without_permission(self) -> None:
        self.assertEqual(dock_keys(RESELLER), ["home", "inbox", "finance", "customers"])
        self.assertEqual(dock_keys(viewer("reseller", perms=("dashboard", "tickets"))),
                         ["home", "inbox", "customers", "tickets"])

    def test_pasarguard_staff_bar_has_only_their_pages(self) -> None:
        self.assertEqual(dock_keys(PG_STAFF), ["home", "inbox", "pg_tickets", "pg_users"])

    def test_bar_never_offers_an_item_the_sidebar_hides(self) -> None:
        for v in (OWNER, RESELLER, PG_STAFF, viewer("reseller", perms=("dashboard",)), viewer("staff")):
            sidebar_keys = {i.key for s in build_sidebar(v, path="/home") for i in s.items}
            keys = dock_keys(v)
            self.assertLessEqual(len(keys), DOCK_SLOTS)
            self.assertTrue(set(keys) <= sidebar_keys, keys)

    def test_menu_is_lit_only_when_the_page_is_not_in_the_bar(self) -> None:
        on_finance = build_dock(build_sidebar(OWNER, path="/service-cancellations"))
        self.assertEqual([i.key for i in on_finance.items if i.active], ["finance"])
        self.assertFalse(on_finance.more_active)
        on_plans = build_dock(build_sidebar(OWNER, path="/plans"))
        self.assertFalse(any(i.active for i in on_plans.items))
        self.assertTrue(on_plans.more_active)

    def test_bar_keeps_the_unread_flag_of_its_items(self) -> None:
        items = {i.key: i for i in build_dock(build_sidebar(OWNER, path="/home")).items}
        self.assertEqual(items["inbox"].dot, "inbox_alert")
        self.assertEqual(items["finance"].dot, "cancel_alert")


class DashboardShortcutTests(unittest.TestCase):
    def test_owner_shortcuts_follow_priority_and_name_their_sidebar_section(self) -> None:
        self.assertEqual(shortcut_rows(OWNER), [
            ("مالی", "فروشگاه", True),
            ("کاربران ربات", "فروشگاه", True),
            ("پلن‌ها", "فروشگاه", False),
            ("پشتیبانی", "فروشگاه", False),
            ("نمایندگان", "فروشگاه", False),
            ("کاربران", "پاسارگارد", False),
            ("نمای کلی فروشگاه", "فروشگاه", False),
            ("نمای کلی پاسارگارد", "پاسارگارد", False),
        ])

    def test_shortcuts_use_the_sidebar_tone_and_never_exceed_the_limit(self) -> None:
        shortcuts = build_shortcuts(build_sidebar(OWNER, path="/home"))
        self.assertEqual(len(shortcuts), DASHBOARD_SHORTCUT_LIMIT)
        tones = {sc.item.key: sc.tone for sc in shortcuts}
        self.assertEqual((tones["finance"], tones["pg_users"]), ("bot", "pg"))

    def test_missing_permission_hides_the_shortcut(self) -> None:
        labels_only = [row[0] for row in shortcut_rows(viewer("reseller", perms=("dashboard",)))]
        for hidden in ("مالی", "پلن‌ها", "پشتیبانی", "تنظیمات فروشگاه", "کاربران ربات"):
            self.assertNotIn(hidden, labels_only)
        self.assertEqual(labels_only[0], "مشتریان")

    def test_marketing_shortcut_opens_the_first_allowed_section(self) -> None:
        shop = viewer("reseller", perms=("dashboard",), marketing=("/catalog-discounts", "/shop-broadcast"))
        marketing = [sc.item for sc in build_shortcuts(build_sidebar(shop, path="/home")) if sc.item.key == "marketing"]
        self.assertEqual([m.href for m in marketing], ["/catalog-discounts"])

    def test_pasarguard_staff_gets_only_granted_shortcuts(self) -> None:
        self.assertEqual(shortcut_rows(PG_STAFF), [("پشتیبانی", "حساب", True), ("کاربران", "پاسارگارد", True)])


def _render_page(staff: dict | None, identity: dict, *, is_owner: bool, path: str = "/home", **ctx) -> str:
    request = SimpleNamespace(query_params={}, url=SimpleNamespace(path=path), state=SimpleNamespace())
    return templates.env.get_template("base.html").render(
        request=request, app_version="t", staff=staff, identity=identity, is_owner=is_owner, **ctx)


class RenderedBottomBarTests(unittest.TestCase):
    OWNER_STAFF = {"role": "admin", "permissions": []}

    def bar(self, html: str) -> str:
        return html.split('data-bottom-dock>')[1].split("</nav>")[0]

    def test_bar_renders_items_in_order_with_more_and_search(self) -> None:
        html = _render_page(self.OWNER_STAFF, {"kind": "owner"}, is_owner=True)
        bar = self.bar(html)
        self.assertEqual(re.findall(r'<a href="([^"]+)" class="bottom-dock-item', bar),
                         ["/home", "/inbox", "/finance", "/users"])
        self.assertIn('aria-current="page"', bar.split('href="/inbox"')[0])
        menu = re.search(r'<button[^>]*data-drawer-open[^>]*>.*?</button>', bar, re.S).group(0)
        self.assertIn('aria-expanded="false"', menu)
        self.assertIn("منو", menu)

    def test_phone_header_has_brand_version_and_search_and_no_hamburger(self) -> None:
        html = _render_page(self.OWNER_STAFF, {"kind": "owner"}, is_owner=True)
        header = html.split('<header class="topbar')[1].split("</header>")[0]
        self.assertRegex(header.split('class="topbar-brand-text"')[1], r'^>\s*<strong dir="auto">[^<]+</strong>')
        self.assertIn('class="topbar-version"', header)
        self.assertIn('id="panel-search-input-top"', header)
        self.assertIn("data-panel-search-input", header)
        self.assertNotIn("menu-toggle", html)

    def test_the_bar_carries_the_drawer_close_button(self) -> None:
        html = _render_page(self.OWNER_STAFF, {"kind": "owner"}, is_owner=True)
        dock = html.split("data-bottom-dock>")[1].split("{# ")[0].split("</div>")[0]
        self.assertIn('class="bottom-dock-close" data-drawer-close aria-controls="sidebar" aria-label="بستن منو"', dock)
        self.assertEqual(html.count("data-drawer-close"), 1)
        # One search field per screen size: the sidebar keeps its own for desktop, with its own ids.
        self.assertEqual(html.count('id="panel-search-input"'), 1)
        self.assertEqual(html.count("data-panel-search-results"), 2)

    def test_unread_dot_is_shown_on_the_bar_item(self) -> None:
        bar = self.bar(_render_page(self.OWNER_STAFF, {"kind": "owner"}, is_owner=True, inbox_alert=True))
        inbox = re.search(r'<a href="/inbox".*?</a>', bar, re.S).group(0)
        self.assertIn('aria-label="اعلان خوانده‌نشده"', inbox)
        quiet = self.bar(_render_page(self.OWNER_STAFF, {"kind": "owner"}, is_owner=True, inbox_alert=False))
        self.assertNotIn("nav-dot", re.search(r'<a href="/inbox".*?</a>', quiet, re.S).group(0))

    def test_menu_is_lit_on_a_page_outside_the_bar(self) -> None:
        bar = self.bar(_render_page(self.OWNER_STAFF, {"kind": "owner"}, is_owner=True, path="/plans"))
        self.assertIn('class="bottom-dock-item is-active" data-drawer-open', bar)
        self.assertNotIn('aria-current="page"', bar)

    def test_signed_out_pages_have_no_bar(self) -> None:
        html = _render_page(None, {}, is_owner=False, path="/login")
        self.assertNotIn("data-bottom-dock", html)


class RenderedShortcutTests(unittest.TestCase):
    def render(self, staff: dict, identity: dict, *, is_owner: bool, **ctx) -> str:
        return templates.env.get_template("_home_shortcuts.html").render(
            staff=staff, identity=identity, is_owner=is_owner, marketing_sections=(), **ctx)

    def test_owner_shortcuts_render_links_sections_and_bar_markers(self) -> None:
        html = self.render({"role": "admin", "permissions": [], "pg_permissions": ["pg_users"]}, {"kind": "owner"},
                           is_owner=True)
        self.assertEqual(re.findall(r'class="home-shortcut [^"]*" href="([^"]+)"', html)[:3],
                         ["/finance", "/users", "/plans"])
        finance = re.search(r'<a class="home-shortcut [^"]*" href="/finance">.*?</a>', html, re.S).group(0)
        self.assertIn("is-in-dock", finance)
        self.assertIn("<small>فروشگاه</small>", finance)
        pg_users = re.search(r'<a class="home-shortcut [^"]*" href="/pg/users">.*?</a>', html, re.S).group(0)
        self.assertIn("home-shortcut-pg", pg_users)
        self.assertNotIn("is-in-dock", pg_users)

    def test_reseller_without_finance_right_gets_no_finance_shortcut(self) -> None:
        html = self.render({"role": "reseller", "permissions": ["dashboard"]}, {"kind": "reseller"}, is_owner=False)
        self.assertNotIn('href="/finance"', html)
        self.assertIn('href="/users"', html)

    def test_cancellation_dot_follows_the_flag(self) -> None:
        staff = {"role": "admin", "permissions": []}
        self.assertIn('aria-label="درخواست لغو در انتظار"',
                      self.render(staff, {"kind": "owner"}, is_owner=True, cancel_alert=True))
        self.assertNotIn("nav-dot", self.render(staff, {"kind": "owner"}, is_owner=True, cancel_alert=False))


if __name__ == "__main__":
    unittest.main()
