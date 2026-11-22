"""Which tools of the «ابزار و پاکسازی» area a signed-in staff member may open, and the area's list page.

The area gathers the cleanup and maintenance pages that used to sit in different places (the service
cleanup page, the order cleanup tab of the finance settings, the backup settings). Each row is shown only
when the route it opens would admit the staff member, using that route's own scope function, so the list
and the sidebar never offer a link that ends in a refusal and never reveal a tool the route would refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.authz import authz_from_staff, can_shop
from app.services.order_purge import purge_scope
from app.services.platform_identity import is_explicit_owner_staff
from app.services.service_cleanup import cleanup_scope
from app.services.settings_hub import HubRow, HubSection

TOOLS_LIST_HREF = "/tools"
TOOLS_TITLE = "ابزار و پاکسازی"
TOOLS_ICON = "tools"

SERVICE_CLEANUP = "service_cleanup"
ORDER_CLEANUP = "order_cleanup"
BACKUP = "backup"


@dataclass(frozen=True, slots=True)
class ToolSection:
    key: str
    label: str
    href: str
    description: str
    icon: str


_SERVICE_CLEANUP = ToolSection(
    SERVICE_CLEANUP, "پاکسازی سرویس‌ها", "/service-cleanup",
    "حذف خودکار سرویس‌های منقضی و بی‌حجم", "trash",
)
# The cleanup tab of the finance settings modal (finance_pages opens it from ``settings=cleanup``).
_ORDER_CLEANUP = ToolSection(
    ORDER_CLEANUP, "پاکسازی سفارش‌ها", "/finance?tab=orders&settings=cleanup",
    "حذف سفارش‌ها و پرداخت‌های رهاشده", "cart",
)
_BACKUP = ToolSection(
    BACKUP, "بکاپ", "/settings?tab=backup",
    "ساخت، بازگردانی و بکاپ زمان‌بندی‌شده", "download",
)

# (group label, section keys in display order); a group with none of them is left out.
_GROUP_LAYOUT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("پاکسازی", (SERVICE_CLEANUP, ORDER_CLEANUP)),
    ("نگهداری", (BACKUP,)),
)


def _can_open_service_cleanup(staff: Mapping[str, Any]) -> bool:
    """Same rule as /service-cleanup and its POST routes (``cleanup_scope``)."""
    try:
        cleanup_scope(dict(staff))
    except PermissionError:
        return False
    return True


def _can_open_order_cleanup(staff: Mapping[str, Any]) -> bool:
    """Same rules as the finance page, its settings modal, and the cleanup tab inside it (``purge_scope``)."""
    authz = authz_from_staff(dict(staff))
    is_admin = staff.get("role") == "admin"
    if not (can_shop(authz, "orders") or can_shop(authz, "payments")):
        return False
    if not (is_admin or can_shop(authz, "shop_settings")):
        return False
    try:
        purge_scope(dict(staff))
    except PermissionError:
        return False
    return True


def _can_open_backup(staff: Mapping[str, Any]) -> bool:
    """Same rule as ``require_admin``, which guards the system settings: ``role=admin`` plus an Owner principal."""
    return staff.get("role") == "admin" and is_explicit_owner_staff(dict(staff))


def tool_sections(staff: Mapping[str, Any] | None) -> tuple[ToolSection, ...]:
    """Tools the staff member can open, in display order. Empty when unauthenticated."""
    if not staff:
        return ()
    allowed = {
        SERVICE_CLEANUP: _can_open_service_cleanup(staff),
        ORDER_CLEANUP: _can_open_order_cleanup(staff),
        BACKUP: _can_open_backup(staff),
    }
    by_key = {s.key: s for s in (_SERVICE_CLEANUP, _ORDER_CLEANUP, _BACKUP)}
    return tuple(by_key[key] for _label, keys in _GROUP_LAYOUT for key in keys if allowed[key])


def tools_hub_sections(sections: tuple[ToolSection, ...]) -> tuple[HubSection, ...]:
    """The list page of the area (DESIGN_SYSTEM.md §6-2): one card per group, one row per allowed tool."""
    keys = {s.key for s in sections}
    by_key = {s.key: s for s in sections}
    hub: list[HubSection] = []
    for label, members in _GROUP_LAYOUT:
        rows = tuple(
            HubRow(label=by_key[m].label, href=by_key[m].href, description=by_key[m].description, icon=by_key[m].icon)
            for m in members
            if m in keys
        )
        if rows:
            hub.append(HubSection(label=label, rows=rows))
    return tuple(hub)
