"""Web panel navigation registry: one list of sections and items for the sidebar and Mini App.

Each item carries the same visibility rule as the route it opens, so the menu never offers a
link that ends in «دسترسی مجاز نیست». Labels follow the panel glossary (docs/NAV_SETTINGS.md).
The phone bottom bar and the dashboard shortcuts are views over the built sidebar, so they show
exactly the items, names, active state and unread dots the sidebar shows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.users import PANEL_SETTINGS_KEYS

SYSTEM = "system"
SHOP = "shop"
PG = "pg"

DOT_INBOX = "inbox_alert"
DOT_TICKETS = "tickets_unread"
DOT_CANCEL = "cancel_alert"

# Accessible name of the unread dot per flag.
DOT_LABELS: dict[str, str] = {
    DOT_INBOX: "اعلان خوانده‌نشده",
    DOT_TICKETS: "پیام خوانده‌نشده",
    DOT_CANCEL: "درخواست لغو در انتظار",
}

MARKETING_KEY = "marketing"

# Phone bottom bar: dashboard and action center, then the most used pages the viewer may open.
DOCK_SLOTS = 4
DOCK_PRIORITY: tuple[str, ...] = (
    "home", "inbox", "finance", "users", "customers", "tickets", "pg_tickets", "pg_users", "plans",
)
# Dashboard shortcuts, most used first; four per row on desktop, the same grid as the KPI strip.
DASHBOARD_SHORTCUT_LIMIT = 8
SHORTCUT_PRIORITY: tuple[str, ...] = (
    "finance", "users", "customers", "plans", "tickets", "pg_tickets", "resellers", "my_resellers",
    "pg_users", "shop_overview", "pg_overview", MARKETING_KEY, "shop_settings", "my_subscription", "pg_nodes",
)


@dataclass(frozen=True, slots=True)
class NavViewer:
    """What the sidebar needs to know about the signed-in staff member."""

    role: str
    perms: frozenset[str]
    pg_perms: frozenset[str]
    is_owner: bool
    can_add_representative: bool
    can_manage_representatives: bool
    marketing_hrefs: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_reseller(self) -> bool:
        return self.role == "reseller"

    @property
    def is_pg_staff(self) -> bool:
        return self.role == "pg_staff"

    @property
    def has_bot(self) -> bool:
        return self.is_admin or bool(self.perms) or self.is_reseller

    @property
    def has_pg(self) -> bool:
        return bool(self.pg_perms)


def viewer_from_context(
    staff: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
    *,
    is_owner: bool,
    marketing_sections: Iterable[Any] = (),
) -> NavViewer:
    staff = staff or {}
    identity = identity or {}
    return NavViewer(
        role=str(staff.get("role") or ""),
        perms=frozenset(str(p) for p in (staff.get("permissions") or [])),
        pg_perms=frozenset(str(p) for p in (staff.get("pg_permissions") or [])),
        is_owner=bool(is_owner),
        can_add_representative=bool(identity.get("can_add_representative")),
        can_manage_representatives=bool(identity.get("can_manage_representatives")),
        marketing_hrefs=tuple(str(s.href) for s in marketing_sections),
    )


@dataclass(frozen=True, slots=True)
class NavLocation:
    path: str
    tab: str

    def is_panel_settings(self) -> bool:
        return self.path.startswith("/settings") and self.tab in PANEL_SETTINGS_KEYS


@dataclass(frozen=True, slots=True)
class NavItem:
    key: str
    label: str
    href: str
    icon: str
    active: bool = False
    dot: str = ""
    short: str = ""

    @property
    def short_label(self) -> str:
        """Label for the narrow phone bar; the glossary name unless a shorter one is registered."""
        return self.short or self.label


@dataclass(frozen=True, slots=True)
class NavSection:
    key: str
    label: str
    tone: str
    items: tuple[NavItem, ...]


Rule = Callable[[NavViewer], bool]
ActiveRule = Callable[[NavLocation], bool]


@dataclass(frozen=True, slots=True)
class NavDock:
    """Phone bottom bar: up to ``DOCK_SLOTS`` items; «منو» is lit when the page lives elsewhere."""

    items: tuple[NavItem, ...]
    more_active: bool


@dataclass(frozen=True, slots=True)
class NavShortcut:
    """A dashboard shortcut with the sidebar section it sits in."""

    item: NavItem
    section_label: str
    tone: str
    in_dock: bool


@dataclass(frozen=True, slots=True)
class _ItemSpec:
    key: str
    label: str
    href: str
    icon: str
    visible: Rule
    active: ActiveRule
    dot: str = ""
    short: str = ""


def _starts(*prefixes: str) -> ActiveRule:
    return lambda loc: any(loc.path.startswith(p) for p in prefixes)


def _exact(path: str) -> ActiveRule:
    return lambda loc: loc.path == path


def _perm(*keys: str) -> Rule:
    return lambda v: v.is_admin or any(k in v.perms for k in keys)


def _pg(key: str) -> Rule:
    return lambda v: key in v.pg_perms


def _owner(v: NavViewer) -> bool:
    return v.is_owner


def _bot_settings_active(loc: NavLocation) -> bool:
    if loc.path.startswith(("/shop-settings", "/message-variables")):
        return True
    return loc.path.startswith("/settings") and not loc.is_panel_settings()


_DASHBOARD = _ItemSpec("home", "داشبورد", "/home", "home", lambda v: True, _exact("/home"))
_INBOX = _ItemSpec("inbox", "مرکز اقدام", "/inbox", "inbox", lambda v: True, _starts("/inbox"), DOT_INBOX)

_SYSTEM_OWNER: tuple[_ItemSpec, ...] = (
    _DASHBOARD,
    _INBOX,
    _ItemSpec("system_settings", "تنظیمات سیستم", "/settings?tab=general", "sliders", _owner,
              lambda loc: loc.is_panel_settings() or loc.path.startswith("/security")),
)

_SYSTEM_STAFF: tuple[_ItemSpec, ...] = (
    _DASHBOARD,
    _INBOX,
    _ItemSpec("account", "حساب و امنیت", "/security", "sliders", lambda v: True, _starts("/security")),
    _ItemSpec("pg_tickets", "پشتیبانی", "/tickets", "ticket", lambda v: v.is_pg_staff, _starts("/tickets"), DOT_TICKETS),
)

_SHOP_ITEMS: tuple[_ItemSpec, ...] = (
    _ItemSpec("shop_overview", "نمای کلی فروشگاه", "/dashboard", "shield-check", _perm("dashboard"), _exact("/dashboard")),
    # A shop owner's own package belongs with the shop, not with the account section.
    _ItemSpec("my_subscription", "اشتراک من", "/my-subscription", "card",
              lambda v: v.is_reseller and not v.is_owner, _starts("/my-subscription")),
    _ItemSpec("users", "کاربران ربات", "/users", "users", _owner, _starts("/users"), short="کاربران"),
    _ItemSpec("customers", "مشتریان", "/users", "users",
              lambda v: not v.is_owner and "dashboard" in v.perms, _starts("/users")),
    _ItemSpec("my_resellers", "نمایندگان من", "/resellers", "users",
              lambda v: not v.is_owner and v.can_add_representative, _starts("/resellers")),
    _ItemSpec("resellers", "نمایندگان", "/resellers", "users-pair",
              lambda v: v.is_owner and v.can_manage_representatives, _starts("/resellers")),
    _ItemSpec("finance", "مالی", "/finance", "cart", _perm("orders", "payments"),
              _starts("/finance", "/orders", "/payments", "/service-cancellations"), DOT_CANCEL),
    _ItemSpec("plans", "پلن‌ها", "/plans", "plans", _perm("plans"), _starts("/plans")),
    _ItemSpec("tickets", "پشتیبانی", "/tickets", "ticket",
              lambda v: v.is_owner or "tickets" in v.perms, _starts("/tickets"), DOT_TICKETS),
    # Placeholder: href and visibility come from marketing_nav (see _marketing_item).
    _ItemSpec(MARKETING_KEY, "بازاریابی", "", "megaphone", lambda v: False, lambda loc: False),
    _ItemSpec("service_cleanup", "پاکسازی سرویس‌ها", "/service-cleanup", "trash",
              lambda v: v.is_owner or "service_cleanup" in v.perms, _starts("/service-cleanup")),
    _ItemSpec("staff_actions", "گزارش کارکنان", "/staff-actions", "receipt",
              lambda v: v.is_owner or v.role in ("reseller", "principal"), _starts("/staff-actions")),
    _ItemSpec("bot_settings", "تنظیمات ربات", "/settings", "bot", _owner, _bot_settings_active),
    _ItemSpec("shop_settings", "تنظیمات فروشگاه", "/shop-settings", "bot",
              lambda v: not v.is_owner and "shop_settings" in v.perms, _bot_settings_active),
)

_PG_ITEMS: tuple[_ItemSpec, ...] = (
    _ItemSpec("pg_overview", "نمای کلی پاسارگارد", "/pg", "shield-check", _pg("pg_overview"), _exact("/pg")),
    _ItemSpec("pg_users", "کاربران", "/pg/users", "user", _pg("pg_users"), _starts("/pg/users")),
    _ItemSpec("pg_admins", "ادمین‌ها", "/pg/admins", "shield", _pg("pg_admins"), _starts("/pg/admins")),
    _ItemSpec("pg_nodes", "نودها", "/pg/nodes", "nodes", _pg("pg_nodes"), _starts("/pg/nodes")),
    _ItemSpec("pg_groups", "گروه‌ها", "/pg/groups", "group", _pg("pg_groups"), _starts("/pg/groups")),
    _ItemSpec("pg_hosts", "هاست‌ها", "/pg/hosts", "globe", _pg("pg_hosts"), _starts("/pg/hosts")),
    _ItemSpec("pg_templates", "تمپلیت‌ها", "/pg/templates", "template", _pg("pg_templates"), _starts("/pg/templates")),
    _ItemSpec("pg_inbounds", "اینباندها", "/pg/inbounds", "inbound", _pg("pg_inbounds"), _starts("/pg/inbounds")),
    _ItemSpec("connection_test", "تست اتصال سرویس", "/connection-status", "pulse", _owner, _starts("/connection-status")),
)

def _items(specs: Iterable[_ItemSpec], viewer: NavViewer, loc: NavLocation) -> list[NavItem]:
    items: list[NavItem] = []
    for spec in specs:
        if spec.key == MARKETING_KEY:
            marketing = _marketing_item(spec, viewer, loc)
            if marketing is not None:
                items.append(marketing)
        elif spec.visible(viewer):
            items.append(NavItem(spec.key, spec.label, spec.href, spec.icon, bool(spec.active(loc)), spec.dot, spec.short))
    return items


def _marketing_item(spec: _ItemSpec, viewer: NavViewer, loc: NavLocation) -> NavItem | None:
    """One entry for every marketing section the viewer may open; it opens the first of them."""
    if not viewer.marketing_hrefs:
        return None
    active = any(loc.path.startswith(href.split("?")[0]) for href in viewer.marketing_hrefs)
    return NavItem(spec.key, spec.label, viewer.marketing_hrefs[0], spec.icon, active)


def build_sidebar(viewer: NavViewer, *, path: str, tab: str = "") -> tuple[NavSection, ...]:
    """Sidebar sections for ``viewer`` at ``path`` (``tab`` is the ``?tab=`` value, if any)."""
    loc = NavLocation(path=path or "/", tab=tab or "")
    system_specs = _SYSTEM_OWNER if viewer.is_owner else _SYSTEM_STAFF
    sections = [NavSection(SYSTEM, "سیستم" if viewer.is_owner else "حساب", "home",
                           tuple(_items(system_specs, viewer, loc)))]
    if viewer.has_bot:
        sections.append(NavSection(SHOP, "فروشگاه", "bot", tuple(_items(_SHOP_ITEMS, viewer, loc))))
    pg_items = tuple(_items(_PG_ITEMS, viewer, loc))
    if viewer.has_pg or pg_items:
        sections.append(NavSection(PG, "پاسارگارد", "pg", pg_items))
    return tuple(sections)


def _items_by_key(sections: Iterable[NavSection]) -> dict[str, tuple[NavItem, NavSection]]:
    return {item.key: (item, section) for section in sections for item in section.items}


def build_dock(sections: Iterable[NavSection]) -> NavDock:
    """Phone bottom bar for the sidebar ``sections`` the viewer was given."""
    by_key = _items_by_key(sections)
    items = tuple(by_key[key][0] for key in DOCK_PRIORITY if key in by_key)[:DOCK_SLOTS]
    return NavDock(items=items, more_active=not any(item.active for item in items))


def build_shortcuts(sections: Iterable[NavSection]) -> tuple[NavShortcut, ...]:
    """Dashboard shortcuts for the sidebar ``sections``; ``in_dock`` marks the ones the phone bar already has."""
    sections = tuple(sections)
    by_key = _items_by_key(sections)
    dock_keys = {item.key for item in build_dock(sections).items}
    picked = [by_key[key] for key in SHORTCUT_PRIORITY if key in by_key][:DASHBOARD_SHORTCUT_LIMIT]
    return tuple(
        NavShortcut(item=item, section_label=section.label, tone=section.tone, in_dock=item.key in dock_keys)
        for item, section in picked
    )


_ALL_SPECS: dict[str, _ItemSpec] = {
    s.key: s for s in (*_SYSTEM_OWNER, *_SYSTEM_STAFF, *_SHOP_ITEMS, *_PG_ITEMS) if s.key != MARKETING_KEY
}

# Mini App «پنل وب» shortcuts in display order: (id the Mini App script knows, registry key).
MINIAPP_OWNER_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("resellers", "resellers"),
    ("finance", "finance"),
    ("users", "users"),
    ("tickets", "tickets"),
    ("dashboard", "shop_overview"),
)
MINIAPP_RESELLER_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("home", "home"),
    ("finance", "finance"),
    ("tickets", "tickets"),
    ("shop", "shop_settings"),
)


def miniapp_shortcuts(pairs: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """Shortcut links with the sidebar's own labels and paths."""
    return [{"id": link_id, "label": _ALL_SPECS[key].label, "path": _ALL_SPECS[key].href} for link_id, key in pairs]


def sidebar_for_template(
    staff: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
    is_owner: bool,
    marketing_sections: Iterable[Any],
    path: str,
    tab: str,
) -> tuple[NavSection, ...]:
    """Jinja entry point used by ``base.html``."""
    viewer = viewer_from_context(staff, identity, is_owner=is_owner, marketing_sections=marketing_sections or ())
    return build_sidebar(viewer, path=path, tab=tab)


def shortcuts_for_template(
    staff: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
    is_owner: bool,
    marketing_sections: Iterable[Any],
) -> tuple[NavShortcut, ...]:
    """Jinja entry point used by the dashboard body."""
    viewer = viewer_from_context(staff, identity, is_owner=is_owner, marketing_sections=marketing_sections or ())
    return build_shortcuts(build_sidebar(viewer, path="/home"))
