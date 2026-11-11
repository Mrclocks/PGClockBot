"""Reseller Mini App ops payload: stats, panel links and billing gated by web_permissions."""

from __future__ import annotations

from typing import Any, Mapping

from app.db.models import ResellerProfile
from app.services.authz import resolve_shop_permissions_from_profile, shop_feature_allowed
from app.services.panel_nav import miniapp_shortcuts

# Mini App panel shortcut id → FEATURE_PERMS key (deny by default when missing).
OPS_PANEL_LINK_PERMS: dict[str, str] = {
    "home": "dashboard",
    "finance": "payments",
    "tickets": "tickets",
    "shop": "shop_settings",
}

# (link_id, panel_nav key) — same labels/paths as the web sidebar.
OPS_PANEL_LINKS: tuple[tuple[str, str], ...] = (
    ("home", "home"),
    ("finance", "finance"),
    ("tickets", "tickets"),
    ("shop", "shop_settings"),
)

EMPTY_STATS: dict[str, int] = {
    "users": 0,
    "orders": 0,
    "pending": 0,
    "services": 0,
    "revenue": 0,
    "plans": 0,
    "tickets": 0,
}


def allowed_shop_permission_keys(profile: ResellerProfile | None) -> list[str]:
    """Sorted FEATURE_PERMS keys this shop may use; empty when inactive/missing."""
    perms = resolve_shop_permissions_from_profile(profile)
    if not perms:
        return []
    return sorted(perms)


def permitted_ops_panel_links(profile: ResellerProfile | None) -> list[dict[str, str]]:
    """Only the panel shortcuts whose FEATURE_PERMS key the shop has."""
    allowed = [
        (link_id, nav_key)
        for link_id, nav_key in OPS_PANEL_LINKS
        if shop_feature_allowed(key=OPS_PANEL_LINK_PERMS[link_id], profile=profile)
    ]
    return miniapp_shortcuts(allowed) if allowed else []


def reseller_ops_payload(
    profile: ResellerProfile | None,
    *,
    stats: Mapping[str, Any] | None,
    panel_base: str,
) -> dict[str, Any]:
    """Filter shop ops by ``web_permissions``; caller supplies already-scoped stats."""
    permissions = allowed_shop_permission_keys(profile)
    has_dashboard = shop_feature_allowed(key="dashboard", profile=profile)
    out_stats = dict(EMPTY_STATS)
    if has_dashboard and stats is not None:
        for key in EMPTY_STATS:
            try:
                out_stats[key] = int(stats.get(key) or 0)
            except (TypeError, ValueError):
                out_stats[key] = 0

    billing = None
    # PAYG credit is overview data — same gate as the home dashboard card.
    if profile is not None and has_dashboard and (profile.billing_mode or "") == "payg":
        billing = {
            "balance": int(profile.billing_balance or 0),
            "suspended": bool(getattr(profile, "billing_suspended_at", None)),
        }

    base = (panel_base or "").rstrip("/")
    return {
        "stats": out_stats,
        "shop": {
            "bot_username": (profile.bot_username if profile else None) or "",
            "active": bool(profile.is_active) if profile else False,
            "billing": billing,
        },
        "panel_links": permitted_ops_panel_links(profile) if base else [],
        "panel_base": base,
        "permissions": permissions,
    }
