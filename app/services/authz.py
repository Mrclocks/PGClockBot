"""Unified authorization — single source of truth for Web, Bot, and API.

Every request must follow:

    Identity → Role → Permission → PG Credential → PG Client → Data Scope → Response

No surface (web panel, Telegram bot, backend API) may invent a parallel
permission model. Call ``decide_pg`` / ``can_shop`` / ``nav_context`` instead.

Security invariants (must never regress):
- Non-admin actors never receive the Owner/sudo PasarGuard token.
- Missing ``pg_*_password_enc`` fails closed (``need_credentials``), never Owner.
- Platform admin (``role == admin``) is the only sudo actor on the web panel.
- Legacy and newly created accounts use the same readiness + ACL path.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable
from urllib.parse import quote

from app.services.pg_access import (
    PG_FEATURE_KEYS,
    enrich_staff_pg_from_role,
    map_pg_role_actions,
    map_pg_role_to_features,
    map_pg_role_writes,
    role_user_actions,
    staff_pg_action,
    staff_pg_writes,
    staff_user_actions,
)
from app.services.pg_credentials import (
    PG_CREDENTIAL_MISSING_MSG,
    staff_pg_client_ready,
)
from app.services.shop_scope import is_platform_admin, shop_owner_id


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------

class PgDecision(str, Enum):
    """Outcome of a PasarGuard feature authorization check."""

    ALLOW = "allow"
    DENY_FEATURE = "deny_feature"
    NEED_CREDENTIALS = "need_credentials"
    DENY = "deny"


ShopPerm = str
PgFeature = str

# Canonical shop feature keys (mirrors ResellerProfile.web_permissions / bot).
SHOP_FEATURE_KEYS: tuple[str, ...] = (
    "dashboard",
    "plans",
    "orders",
    "payments",
    "tickets",
    "stats",
    "shop_settings",
)

# Platform-admin-only PG nav (not in limited-admin PG_FEATURE_KEYS).
PG_ADMIN_ONLY_FEATURES: tuple[str, ...] = ("pg_admins",)

# Web path for each PG feature (menus + deny redirects).
PG_FEATURE_PATHS: dict[str, str] = {
    "pg_overview": "/pg",
    "pg_users": "/pg/users",
    "pg_templates": "/pg/templates",
    "pg_groups": "/pg/groups",
    "pg_hosts": "/pg/hosts",
    "pg_inbounds": "/pg/inbounds",
    "pg_nodes": "/pg/nodes",
    "pg_admins": "/pg/admins",
}

CREDENTIALS_REQUIRED_PATH = "/pg/credentials-required"


# ---------------------------------------------------------------------------
# Identity / Role helpers
# ---------------------------------------------------------------------------

def actor_role(actor: dict | None) -> str | None:
    if not actor:
        return None
    role = actor.get("role")
    return str(role) if role else None


def is_reseller(actor: dict | None) -> bool:
    return actor_role(actor) == "reseller"


def is_pg_staff(actor: dict | None) -> bool:
    return actor_role(actor) == "pg_staff"


def is_limited_pg_actor(actor: dict | None) -> bool:
    """Reseller or pg_staff — must use own PG credentials, never Owner."""
    return actor_role(actor) in {"reseller", "pg_staff"}


# Re-export shop scope helpers so callers import from one place.
__all__ = (
    "PgDecision",
    "SHOP_FEATURE_KEYS",
    "PG_FEATURE_KEYS",
    "PG_FEATURE_PATHS",
    "CREDENTIALS_REQUIRED_PATH",
    "PG_CREDENTIAL_MISSING_MSG",
    "actor_role",
    "is_platform_admin",
    "is_reseller",
    "is_pg_staff",
    "is_limited_pg_actor",
    "shop_owner_id",
    "shop_permissions",
    "can_shop",
    "pg_features",
    "client_ready",
    "decide_pg",
    "can_pg",
    "can_pg_write",
    "can_pg_action",
    "user_actions",
    "nav_context",
    "first_allowed_pg_path",
    "credentials_required_url",
    "enrich_actor_pg_acl",
    "assert_staff_pg_session",
)


# ---------------------------------------------------------------------------
# Shop permissions (web + bot identical)
# ---------------------------------------------------------------------------

def shop_permissions(actor: dict | None) -> list[str]:
    """Normalized shop feature list from the live staff/session dict."""
    if is_platform_admin(actor):
        return list(SHOP_FEATURE_KEYS)
    raw = (actor or {}).get("permissions") or []
    if isinstance(raw, str):
        from app.services.resellers import parse_perms

        return list(parse_perms(raw) or [])
    return [str(x) for x in raw if x]


def can_shop(actor: dict | None, perm: str) -> bool:
    """True when actor may use a shop (bot catalog) feature.

    Web ``require_perm`` and bot ``has_bot_perm`` must agree with this.
    """
    if not actor or not perm:
        return False
    if is_platform_admin(actor):
        return True
    if is_pg_staff(actor):
        # pg_staff has no shop catalog — only PG + tickets (handled elsewhere).
        return False
    return perm in shop_permissions(actor)


def can_shop_profile(profile: Any, perm: str, *, role: str | None = None) -> bool:
    """Shop check from a ResellerProfile ORM row (bot handlers)."""
    from app.services.resellers import has_perm

    return has_perm(profile, perm, role=role)


# ---------------------------------------------------------------------------
# PG permissions + credential readiness
# ---------------------------------------------------------------------------

def pg_features(actor: dict | None) -> list[str]:
    """Mapped PG panel feature keys for this actor (may be empty)."""
    if is_platform_admin(actor):
        return list(PG_FEATURE_KEYS) + list(PG_ADMIN_ONLY_FEATURES)
    raw = (actor or {}).get("pg_permissions") or []
    out: list[str] = []
    seen: set[str] = set()
    for key in raw:
        k = str(key)
        if k in seen:
            continue
        if k in PG_FEATURE_KEYS or k in PG_ADMIN_ONLY_FEATURES:
            seen.add(k)
            out.append(k)
    return out


def client_ready(actor: dict | None) -> bool:
    """True when the actor may open a PasarGuard API client.

    Platform admin → always (Owner client).
    Others → stored encrypted password present (``pg_client_ready`` flag).
    """
    return staff_pg_client_ready(actor)


def decide_pg(actor: dict | None, feature: str) -> PgDecision:
    """Single PG feature gate used by Web routes, menus, and Bot parity checks.

    Order: identity → role shortcut → feature membership → credential readiness.
    """
    if not actor or not feature:
        return PgDecision.DENY
    if is_platform_admin(actor):
        return PgDecision.ALLOW
    features = pg_features(actor)
    if feature not in features:
        return PgDecision.DENY_FEATURE
    if not client_ready(actor):
        return PgDecision.NEED_CREDENTIALS
    return PgDecision.ALLOW


def can_pg(actor: dict | None, feature: str) -> bool:
    return decide_pg(actor, feature) == PgDecision.ALLOW


def can_pg_write(actor: dict | None, resource: str) -> bool:
    if is_platform_admin(actor):
        return True
    return bool(staff_pg_writes(actor or {}).get(resource))


def can_pg_action(actor: dict | None, resource: str, action: str) -> bool:
    return staff_pg_action(actor or {}, resource, action)


def user_actions(actor: dict | None) -> dict[str, bool]:
    return staff_user_actions(actor or {})


# ---------------------------------------------------------------------------
# Menus / navigation (Web + Bot must mirror)
# ---------------------------------------------------------------------------

def nav_context(actor: dict | None) -> dict[str, Any]:
    """Flags for sidebar / bot PG submenu — derived only from decide_pg rules."""
    role = actor_role(actor)
    admin = is_platform_admin(actor)
    features = pg_features(actor)
    ready = client_ready(actor)
    has_pg_features = admin or bool(features)
    # Visible PG links only when both feature + credentials allow data access.
    visible_pg = [f for f in features if can_pg(actor, f)] if not admin else features
    if admin:
        visible_pg = list(PG_FEATURE_KEYS) + list(PG_ADMIN_ONLY_FEATURES)
    shop = shop_permissions(actor)
    return {
        "role": role,
        "is_admin": admin,
        "is_reseller": role == "reseller",
        "is_pg_staff": role == "pg_staff",
        "shop_permissions": shop,
        "pg_permissions": features,
        "pg_visible": visible_pg,
        "pg_client_ready": ready,
        "has_pg_features": has_pg_features,
        "has_pg": bool(visible_pg) if not admin else True,
        "pg_needs_credentials": has_pg_features and (not ready) and (not admin),
        "has_bot_section": admin or role == "reseller" or bool(shop),
    }


def first_allowed_pg_path(actor: dict | None) -> str:
    """Best landing page after a PG feature deny (live ACL, not cookie)."""
    for key in (
        "pg_overview",
        "pg_users",
        "pg_templates",
        "pg_groups",
        "pg_hosts",
        "pg_nodes",
        "pg_inbounds",
        "pg_admins",
    ):
        if can_pg(actor, key):
            return PG_FEATURE_PATHS[key]
    # Has features but no credentials → configuration page, not silent empty.
    if nav_context(actor).get("pg_needs_credentials"):
        return credentials_required_url()
    if is_platform_admin(actor):
        return "/pg"
    return "/logout"


def credentials_required_url(*, err: str | None = None) -> str:
    msg = err or PG_CREDENTIAL_MISSING_MSG
    return f"{CREDENTIALS_REQUIRED_PATH}?err={quote(msg, safe='')}"


# ---------------------------------------------------------------------------
# ACL enrichment (login + require_staff share this — old/new accounts identical)
# ---------------------------------------------------------------------------

def enrich_actor_pg_acl(
    actor: dict,
    *,
    features: Iterable[str] | None,
    role: dict | None,
    pg_client_ready: bool,
    pg_admin_username: str | None = None,
    pg_role_id: int | None = None,
) -> dict:
    """Attach live PG ACL onto a staff dict. Always overwrites cookie ACL."""
    out = enrich_staff_pg_from_role(actor, list(features or []), role)
    if pg_admin_username is not None:
        uname = str(pg_admin_username or "").strip()
        if uname:
            out["pg_admin_username"] = uname
        else:
            out.pop("pg_admin_username", None)
    if pg_role_id is not None:
        try:
            out["pg_role_id"] = int(pg_role_id)
        except (TypeError, ValueError):
            out.pop("pg_role_id", None)
    elif "pg_role_id" not in out:
        pass
    # Platform admin is always ready; limited actors use the flag.
    if is_platform_admin(out):
        out["pg_client_ready"] = True
    else:
        out["pg_client_ready"] = bool(pg_client_ready)
    return out


def clear_stale_pg_acl(actor: dict) -> dict:
    """Drop cookie-sourced PG fields before live resolve."""
    out = dict(actor)
    for key in (
        "pg_permissions",
        "pg_writes",
        "pg_actions",
        "pg_user_actions",
        "pg_access",
        "pg_role_id",
        "pg_client_ready",
    ):
        out.pop(key, None)
    return out


async def load_live_pg_acl_for_role_id(pg_role_id: int | None) -> tuple[list[str], dict | None]:
    """Fetch + map a PasarGuard role — shared by web require_staff and login."""
    from app.services.pg_access import resolve_reseller_pg_features

    if not pg_role_id:
        return [], None
    return await resolve_reseller_pg_features(int(pg_role_id))


async def enrich_reseller_actor(session: Any, actor: dict, profile: Any) -> dict:
    """Live shop + PG ACL for a reseller — identical for legacy and new accounts."""
    from app.services.pg_credentials import enc_has_secret
    from app.services.resellers import (
        DEFAULT_FEATURE_PERMS,
        parse_perms,
        with_shop_settings,
    )

    user = clear_stale_pg_acl(actor)
    raw_perms = profile.web_permissions
    if raw_perms is None:
        user["permissions"] = with_shop_settings(parse_perms(DEFAULT_FEATURE_PERMS))
    else:
        parsed = parse_perms(raw_perms)
        user["permissions"] = with_shop_settings(parsed) if parsed else parsed
    user["bot_user_id"] = int(profile.user_id)
    user["role"] = "reseller"

    pg_uname = (profile.pg_admin_username or "").strip() or None
    pg_role_id = int(profile.pg_role_id) if profile.pg_role_id else None
    features, role = await load_live_pg_acl_for_role_id(pg_role_id)
    ready = bool(pg_uname and enc_has_secret(profile.pg_admin_password_enc))
    return enrich_actor_pg_acl(
        user,
        features=features,
        role=role,
        pg_client_ready=ready,
        pg_admin_username=pg_uname,
        pg_role_id=pg_role_id,
    )


async def enrich_pg_staff_actor(session: Any, actor: dict, row: Any) -> dict:
    """Live PG ACL for pg_staff — identical for legacy and new accounts."""
    from app.services.pg_credentials import enc_has_secret
    from app.services.pg_staff_access import resolve_pg_role_id_for_admin

    user = clear_stale_pg_acl(actor)
    user["permissions"] = []
    user["role"] = "pg_staff"
    user["pg_staff_id"] = int(row.id)
    pg_uname = (row.pg_username or "").strip()
    user["pg_admin_username"] = pg_uname
    role_id = await resolve_pg_role_id_for_admin(pg_uname)
    features, role = await load_live_pg_acl_for_role_id(role_id)
    ready = enc_has_secret(row.pg_password_enc)
    return enrich_actor_pg_acl(
        user,
        features=features,
        role=role,
        pg_client_ready=ready,
        pg_admin_username=pg_uname,
        pg_role_id=role_id,
    )


# ---------------------------------------------------------------------------
# PG client / scope guards
# ---------------------------------------------------------------------------

def assert_staff_pg_session(actor: dict | None, session: Any) -> None:
    """Non-admin PG ops require a DB session to resolve credentials.

    Prevents the historical ``session is None → get_pg() Owner`` regression.
    """
    from app.services.pasarguard import PasarGuardError

    if is_platform_admin(actor):
        return
    if session is None:
        raise PasarGuardError(
            "جلسه پایگاه‌داده برای دسترسی پاسارگارد لازم است — توکن Owner مجاز نیست"
        )
    if not client_ready(actor):
        raise PasarGuardError(PG_CREDENTIAL_MISSING_MSG)


async def resolve_pg_client(session: Any, actor: dict):
    """Return ``(client, as_owner)`` — sole entry for request-scoped PG access.

    Thin wrapper over ``get_pg_for_staff`` that enforces the session guard first.
    """
    from app.services.pasarguard import get_pg_for_staff

    assert_staff_pg_session(actor, session)
    return await get_pg_for_staff(session, actor)


def bot_pg_feature_entries(actor: dict | None = None) -> list[tuple[str, str]]:
    """PG submenu entries for Telegram — same feature gates as the web panel.

    Platform admin (bot) → full list (Owner client). Limited actors (if ever
    exposed on Telegram) see only features ``can_pg`` allows. Callers must still
    gate the hub with platform-admin checks today.
    """
    # (reply_action_key, label, required_pg_feature)
    catalog = [
        ("pg_stats", "🏠 نمای کلی", "pg_overview"),
        ("pg_users", "👥 کاربران VPN", "pg_users"),
        ("pg_create", "➕ ساخت کاربر", "pg_users"),
        ("pg_search", "🔎 جستجوی یوزر", "pg_users"),
        ("pg_nodes", "🕸 نودها", "pg_nodes"),
        ("pg_group", "📁 ساخت گروه", "pg_groups"),
        ("pg_template", "📋 ساخت تمپلیت", "pg_templates"),
    ]
    if actor is None or is_platform_admin(actor):
        return [(key, label) for key, label, _feat in catalog]
    out: list[tuple[str, str]] = []
    for key, label, feat in catalog:
        if can_pg(actor, feat):
            out.append((key, label))
    return out


def apply_role_dict_to_features(role: dict | None) -> list[str]:
    """Map a raw PasarGuard role payload to feature keys (shared helper)."""
    return map_pg_role_to_features(role)


def role_write_matrix(role: dict | None) -> dict[str, bool]:
    return map_pg_role_writes(role)


def role_action_matrix(role: dict | None) -> dict[str, dict[str, bool]]:
    return map_pg_role_actions(role)


def role_user_action_matrix(role: dict | None) -> dict[str, bool]:
    return role_user_actions(role)
