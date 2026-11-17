"""Unified authorization decision layer (Phase C0 / D4 / Hybrid Owner PG).

Web, Bot, and API must ask the same allow/deny questions here.
Shop feature keys use ``web_permissions`` (``bot_permissions`` is a mirrored
column only — never read for decisions).

Hybrid / legacy:
- ``role==admin`` keeps full *shop feature* allow via ``can_shop`` (SAFE LEGACY
  UI/menu parity — C0). Cross-tenant / platform catalog uses
  ``shop_scope.is_platform_admin`` → explicit Owner Principal (Phase 1G).
- Org hierarchy Owner is explicit Principal depth 0 only (Phase 1B).
- PasarGuard pages/actions use mapped ``pg_permissions`` / action matrices
  (fail-closed when empty). Enrichment lives in ``pg_access``.

Phase 1B: Principal fields + ``authorize()`` foundation. Missing principal or
scope never falls back to global org scope. PG role *names* do not affect scope.

See ``docs/PHASE_D4_IDENTITY_MATRIX.md`` and ``app.services.platform_identity``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class PrincipalKind(str, Enum):
    """Logical principal. Explicit Owner uses principal depth 0 (Phase 1B)."""

    OWNER = "owner"
    PLATFORM_ADMIN = "platform_admin"
    RESELLER = "reseller"
    PG_STAFF = "pg_staff"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthzContext:
    kind: PrincipalKind
    role: str
    username: str | None = None
    shop_owner_id: int | None = None
    pg_admin_username: str | None = None
    pg_role_id: int | None = None
    shop_permissions: frozenset[str] = field(default_factory=frozenset)
    pg_permissions: frozenset[str] = field(default_factory=frozenset)
    pg_actions: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)
    pg_user_actions: Mapping[str, bool] = field(default_factory=dict)
    pg_writes: Mapping[str, bool] = field(default_factory=dict)
    pg_access: Mapping[str, Any] = field(default_factory=dict)
    # Phase 1B — server-attached OrgPrincipal (never trust client cookies)
    principal_id: int | None = None
    parent_id: int | None = None
    depth: int | None = None
    principal_status: str | None = None
    visible_principal_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    reason: str = ""


def principal_kind_from_role(role: str | None) -> PrincipalKind:
    """Map session/bot role string → PrincipalKind.

    ``role=admin`` alone is PLATFORM_ADMIN, not Org Owner.
    ``role=principal`` is a Level-1 OrgPrincipal web identity (Phase 2B).
    """
    r = (role or "").strip()
    if r == "admin":
        return PrincipalKind.PLATFORM_ADMIN
    if r == "reseller":
        return PrincipalKind.RESELLER
    if r == "pg_staff":
        return PrincipalKind.PG_STAFF
    if r == "principal":
        return PrincipalKind.RESELLER  # depth-1 org member; not Owner/platform
    return PrincipalKind.UNKNOWN


def is_platform_admin(ctx: AuthzContext) -> bool:
    """Legacy session role admin / platform_admin kind — shop bypass (C0)."""
    return ctx.role == "admin" or ctx.kind in {
        PrincipalKind.OWNER,
        PrincipalKind.PLATFORM_ADMIN,
    }


def is_explicit_org_owner(ctx: AuthzContext) -> bool:
    """True only with an active depth-0 Owner principal on the context."""
    if ctx.principal_id is None or ctx.depth != 0:
        return False
    if ctx.parent_id is not None:
        return False
    if (ctx.principal_status or "") != "active":
        return False
    return True


def is_explicit_owner_staff(staff: Mapping[str, Any] | None) -> bool:
    """Staff-dict Owner check — never ``role=admin`` alone (C2)."""
    from app.services.platform_identity import is_explicit_owner_staff as _fn

    return _fn(staff)


def has_active_org_principal(ctx: AuthzContext) -> bool:
    if ctx.principal_id is None or ctx.depth is None:
        return False
    if (ctx.principal_status or "") != "active":
        return False
    if ctx.depth < 0 or ctx.depth > 2:
        return False
    return True


def has_org_global_scope(ctx: AuthzContext) -> bool:
    """Organization-wide visibility — explicit Owner principal only."""
    return is_explicit_org_owner(ctx) and bool(ctx.visible_principal_ids)


def can_shop(ctx: AuthzContext, key: str) -> bool:
    """Shop feature key (dashboard, plans, orders, …).

    SAFE LEGACY: ``role=admin`` / platform_admin kind still allow all shop
    *feature keys* (menus). Tenant data / platform catalog require explicit
    Owner via ``shop_scope.is_platform_admin`` (Phase 1G).
    """
    if is_platform_admin(ctx):
        return True
    return key in ctx.shop_permissions


def can_pg_page(ctx: AuthzContext, key: str) -> bool:
    """PasarGuard panel page key (pg_users, pg_hosts, …)."""
    return key in ctx.pg_permissions


def can_pg_action(ctx: AuthzContext, resource: str, action: str) -> bool:
    """Exact PG resource action (e.g. hosts.create). Fail closed when matrix missing."""
    block = ctx.pg_actions.get(resource) if isinstance(ctx.pg_actions, Mapping) else None
    if isinstance(block, Mapping) and action in block:
        return bool(block.get(action))
    return False


def can_pg_user_action(ctx: AuthzContext, action: str) -> bool:
    """Fine-grained VPN user action. Matches prior staff_user_actions semantics."""
    raw = ctx.pg_user_actions if isinstance(ctx.pg_user_actions, Mapping) else {}
    if action in ("disable", "enable"):
        return bool(raw.get(action) or raw.get("update"))
    return bool(raw.get(action))


def can_pg_write_resource(ctx: AuthzContext, resource: str) -> bool:
    """Broad write flag (any mutate) — legacy pg_writes parity."""
    return bool(ctx.pg_writes.get(resource)) if isinstance(ctx.pg_writes, Mapping) else False


def _positive_int(value: Any) -> int | None:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def authorize(
    *,
    authenticated: bool,
    ctx: AuthzContext,
    resource_principal_id: int | None,
    pg_permission_ok: bool,
    local_safety_ok: bool = True,
) -> AuthDecision:
    """Central Phase 1B formula (foundation).

    AUTHENTICATED ∧ ACTIVE_PRINCIPAL ∧ RESOURCE_IN_SCOPE
    ∧ PASARGUARD_PERMISSION ∧ LOCAL_SAFETY_POLICY

    Missing principal or empty/unknown scope → deny (never global fallback).
    Callers supply ``pg_permission_ok`` from existing capability matrices;
    PG role *names* are not consulted here.
    """
    if not authenticated:
        return AuthDecision(False, "unauthenticated")
    if not has_active_org_principal(ctx):
        return AuthDecision(False, "inactive_or_missing_principal")
    if not ctx.visible_principal_ids:
        return AuthDecision(False, "missing_scope")
    from app.services.org_scope import principal_in_scope

    if not principal_in_scope(ctx.visible_principal_ids, resource_principal_id):
        return AuthDecision(False, "resource_out_of_scope")
    if not pg_permission_ok:
        return AuthDecision(False, "pg_permission_denied")
    if not local_safety_ok:
        return AuthDecision(False, "local_safety_denied")
    return AuthDecision(True, "ok")


def authz_from_bot_staff(staff: Mapping[str, Any] | None) -> AuthzContext:
    """Build AuthzContext from Bot-resolved staff (same fields as Web)."""
    return authz_from_staff(staff)


def authz_from_staff(staff: Mapping[str, Any] | None) -> AuthzContext:
    """Build context from a resolved web staff dict (post require_staff enrichment)."""
    if not staff:
        return AuthzContext(kind=PrincipalKind.UNKNOWN, role="")
    role = str(staff.get("role") or "")
    kind = principal_kind_from_role(role)
    depth = _optional_int(staff.get("org_depth"))
    principal_id = _optional_int(staff.get("org_principal_id"))
    parent_id = _optional_int(staff.get("org_parent_id"))
    principal_status = (
        str(staff.get("org_status")).strip() if staff.get("org_status") is not None else None
    )
    if (
        principal_id
        and depth == 0
        and parent_id is None
        and (principal_status or "active") == "active"
    ):
        kind = PrincipalKind.OWNER
    shop_owner: int | None = None
    if role == "reseller":
        shop_owner = _positive_int(staff.get("bot_user_id"))
    pg_role = staff.get("pg_role_id")
    try:
        pg_role_id = int(pg_role) if pg_role is not None else None
    except (TypeError, ValueError):
        pg_role_id = None
    raw_visible = staff.get("org_visible_principal_ids") or []
    try:
        visible = frozenset(int(x) for x in raw_visible)
    except (TypeError, ValueError):
        visible = frozenset()
    return AuthzContext(
        kind=kind,
        role=role,
        username=(str(staff.get("username")).strip() if staff.get("username") else None),
        shop_owner_id=shop_owner,
        pg_admin_username=(
            str(staff["pg_admin_username"]).strip() if staff.get("pg_admin_username") else None
        ),
        pg_role_id=pg_role_id,
        shop_permissions=frozenset(staff.get("permissions") or []),
        pg_permissions=frozenset(staff.get("pg_permissions") or []),
        pg_actions=dict(staff.get("pg_actions") or {}),
        pg_user_actions=dict(staff.get("pg_user_actions") or {}),
        pg_writes=dict(staff.get("pg_writes") or {}),
        pg_access=dict(staff.get("pg_access") or {}),
        principal_id=principal_id,
        parent_id=parent_id,
        depth=depth,
        principal_status=principal_status,
        visible_principal_ids=visible,
    )


def authz_from_shop_perm_list(
    perms: list[str] | None,
    *,
    role: str | None = None,
    active: bool = True,
) -> AuthzContext:
    """Build a shop-only context (bot/profile path after permission CSV resolved)."""
    if role == "admin":
        return AuthzContext(
            kind=PrincipalKind.PLATFORM_ADMIN,
            role="admin",
            shop_permissions=frozenset(perms or []),
        )
    if not active:
        return AuthzContext(
            kind=principal_kind_from_role(role or "reseller"),
            role=role or "reseller",
            shop_permissions=frozenset(),
        )
    return AuthzContext(
        kind=principal_kind_from_role(role or "reseller"),
        role=role or "reseller",
        shop_permissions=frozenset(perms or []),
    )


def resolve_shop_permissions_from_profile(profile: Any) -> list[str] | None:
    """Mirror has_perm profile → list resolution.

    Returns:
      None → profile missing/inactive (deny)
      list (possibly empty) → permission set to check

    Rules (Web + Bot must match):
      - ``web_permissions is None`` → DEFAULT + core shop keys
      - ``web_permissions == ""`` / empty parse → intentional deny (``[]``)
      - non-empty → parsed + core shop keys via ``with_shop_settings``
    Source of truth is ``web_permissions`` (``bot_permissions`` is a mirrored column).
    """
    if not profile or not getattr(profile, "is_active", False):
        return None
    from app.services.resellers import (
        DEFAULT_FEATURE_PERMS,
        parse_perms,
        with_shop_settings,
    )

    raw = getattr(profile, "web_permissions", None)
    if raw is None:
        return with_shop_settings(parse_perms(DEFAULT_FEATURE_PERMS))
    parsed = parse_perms(raw)
    return with_shop_settings(parsed) if parsed else parsed


def authz_from_profile(profile: Any, *, role: str | None = None) -> AuthzContext:
    """Build AuthzContext from a ResellerProfile (Bot + Web shop path)."""
    if role == "admin":
        return authz_from_shop_perm_list(None, role="admin")
    perms = resolve_shop_permissions_from_profile(profile)
    if perms is None:
        return authz_from_shop_perm_list([], role=role or "reseller", active=False)
    return authz_from_shop_perm_list(perms, role=role or "reseller", active=True)


def shop_feature_allowed(
    *,
    key: str,
    profile: Any = None,
    staff: Mapping[str, Any] | None = None,
    role: str | None = None,
) -> bool:
    """Single shop-feature decision for Web menus/API and Bot menus/actions.

    Prefer ``staff`` (already resolved session) when available; else ``profile``.
    """
    if key == "approve_receipts":
        key = "payments"
    if staff is not None:
        return can_shop(authz_from_staff(staff), key)
    if role == "admin":
        return can_shop(authz_from_shop_perm_list(None, role="admin"), key)
    return can_shop(authz_from_profile(profile, role=role), key)


def shop_menu_keys(
    *,
    profile: Any = None,
    staff: Mapping[str, Any] | None = None,
    role: str | None = None,
) -> frozenset[str]:
    """Feature keys visible in Web sidebar / Bot reseller hub (same set)."""
    if staff is not None:
        ctx = authz_from_staff(staff)
        if is_platform_admin(ctx):
            from app.services.resellers import FEATURE_PERMS

            return frozenset(k for k, _ in FEATURE_PERMS)
        return ctx.shop_permissions
    if role == "admin":
        from app.services.resellers import FEATURE_PERMS

        return frozenset(k for k, _ in FEATURE_PERMS)
    ctx = authz_from_profile(profile, role=role)
    if is_platform_admin(ctx):
        from app.services.resellers import FEATURE_PERMS

        return frozenset(k for k, _ in FEATURE_PERMS)
    return ctx.shop_permissions
