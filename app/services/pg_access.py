from __future__ import annotations

"""Map PasarGuard admin-role permissions → lightweight web-panel feature keys.

Resellers never get PG settings / admin_roles / cores / api_keys in our UI.
"""

import time
from typing import Any

# Our panel feature keys (shown in sidebar under «پاسارگارد»)
PG_FEATURE_KEYS = (
    "pg_overview",
    "pg_users",
    "pg_templates",
    "pg_groups",
    "pg_hosts",
    "pg_inbounds",
    "pg_nodes",
)

PG_FEATURE_LABELS: dict[str, str] = {
    "pg_overview": "نمای کلی",
    "pg_users": "کاربران",
    "pg_templates": "تمپلیت",
    "pg_groups": "گروه",
    "pg_hosts": "هاست",
    "pg_inbounds": "اینباند",
    "pg_nodes": "نود",
    "pg_admins": "ادمین",
}

# Short-lived cache: role_id → (monotonic_at, features, raw_role)
_ROLE_CACHE: dict[int, tuple[float, list[str], dict]] = {}
_ROLE_CACHE_TTL = 60.0


def invalidate_role_cache(role_id: int | None = None) -> None:
    """Drop one (or all) cached PG role ACL snapshots after role changes."""
    global _ROLE_CACHE
    if role_id is None:
        _ROLE_CACHE = {}
        return
    _ROLE_CACHE.pop(int(role_id), None)


def _action_allowed(value: Any) -> bool:
    """PasarGuard action: True | {scope: N>0} → allowed; else denied."""
    if value is True:
        return True
    if isinstance(value, dict):
        try:
            return int(value.get("scope") or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


def _resource_allows(perms: dict | None, resource: str, *actions: str) -> bool:
    if not isinstance(perms, dict):
        return False
    block = perms.get(resource)
    if not isinstance(block, dict):
        return False
    for action in actions:
        if _action_allowed(block.get(action)):
            return True
    return False


def map_pg_role_to_features(role: dict | None) -> list[str]:
    """Convert a PasarGuard AdminRoleResponse (or similar) into our feature keys."""
    if not role:
        return []
    if role.get("is_owner"):
        return list(PG_FEATURE_KEYS)

    raw = role.get("permissions") or {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        return []

    out: list[str] = []
    if _resource_allows(raw, "system", "read"):
        out.append("pg_overview")
    if _resource_allows(raw, "users", "read", "read_simple", "create", "update", "delete"):
        out.append("pg_users")
    if _resource_allows(raw, "templates", "read", "read_simple", "create", "update", "delete"):
        out.append("pg_templates")
    if _resource_allows(raw, "groups", "read", "read_simple", "create", "update", "delete"):
        out.append("pg_groups")
    if _resource_allows(raw, "hosts", "read", "create", "update"):
        out.append("pg_hosts")
    # Inbounds are typically needed when managing groups — expose with groups or hosts read
    if "pg_groups" in out or "pg_hosts" in out or _resource_allows(raw, "groups", "read_simple"):
        out.append("pg_inbounds")
    if _resource_allows(raw, "nodes", "read", "read_simple", "reconnect", "stats"):
        out.append("pg_nodes")
    # de-dupe preserve order
    seen: set[str] = set()
    ordered = []
    for k in out:
        if k not in seen and k in PG_FEATURE_KEYS:
            seen.add(k)
            ordered.append(k)
    return ordered


def role_user_actions(role: dict | None) -> dict[str, bool]:
    """Fine-grained user actions for the VPN users UI."""
    empty = {
        "create": False,
        "read": False,
        "update": False,
        "delete": False,
        "reset_usage": False,
        "revoke_sub": False,
        "disable": False,
        "enable": False,
    }
    if not role:
        return empty
    if role.get("is_owner"):
        return {k: True for k in empty}

    raw = role.get("permissions") or {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        return empty
    users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
    can_update = _action_allowed(users.get("update"))
    return {
        "create": _action_allowed(users.get("create")),
        "read": _action_allowed(users.get("read")) or _action_allowed(users.get("read_simple")),
        "update": can_update,
        "delete": _action_allowed(users.get("delete")),
        "reset_usage": _action_allowed(users.get("reset_usage")),
        "revoke_sub": _action_allowed(users.get("revoke_sub")),
        "disable": can_update,
        "enable": can_update,
    }


def map_pg_role_actions(role: dict | None) -> dict[str, dict[str, bool]]:
    """Exact PasarGuard action matrix per resource (create/update/delete/reconnect)."""
    resources = {
        "users": ("create", "update", "delete"),
        "templates": ("create", "update", "delete"),
        "groups": ("create", "update", "delete"),
        "hosts": ("create", "update", "delete"),
        "nodes": ("create", "update", "reconnect"),
    }
    empty = {res: {act: False for act in acts} for res, acts in resources.items()}
    if not role:
        return empty
    if role.get("is_owner"):
        return {res: {act: True for act in acts} for res, acts in resources.items()}
    raw = role.get("permissions") or {}
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        return empty
    out: dict[str, dict[str, bool]] = {}
    for res, acts in resources.items():
        block = raw.get(res) if isinstance(raw.get(res), dict) else {}
        out[res] = {act: _action_allowed(block.get(act)) for act in acts}
    return out


def map_pg_role_writes(role: dict | None) -> dict[str, bool]:
    """Which PG resources the role may mutate (any of create/update/delete/reconnect)."""
    actions = map_pg_role_actions(role)
    return {res: any(flags.values()) for res, flags in actions.items()}


def role_access_limits(role: dict | None) -> dict:
    """Template/group allow-lists from role.access."""
    if not role:
        return {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None}
    access = role.get("access") or {}
    if hasattr(access, "model_dump"):
        access = access.model_dump()
    if not isinstance(access, dict):
        access = {}
    return {
        "require_template": bool(access.get("require_template")),
        "allowed_template_ids": access.get("allowed_template_ids"),
        "allowed_group_ids": access.get("allowed_group_ids"),
    }


async def resolve_reseller_pg_features(pg_role_id: int | None) -> tuple[list[str], dict | None]:
    """Fetch role from PasarGuard and return (feature_keys, raw_role). Cached ~60s."""
    if not pg_role_id:
        return [], None
    rid = int(pg_role_id)
    now = time.monotonic()
    hit = _ROLE_CACHE.get(rid)
    if hit and (now - hit[0]) < _ROLE_CACHE_TTL:
        return list(hit[1]), dict(hit[2]) if isinstance(hit[2], dict) else hit[2]

    from app.services.pasarguard import get_pg

    role = None
    try:
        role = await get_pg().get_admin_role(rid)
    except Exception:
        # Fallback: try list and find by id
        try:
            roles = await get_pg().get_admin_roles()
            role = next((r for r in roles if int(r.get("id") or 0) == rid), None)
        except Exception:
            return [], None
    if not isinstance(role, dict):
        return [], None
    features = map_pg_role_to_features(role)
    _ROLE_CACHE[rid] = (now, list(features), dict(role))
    return features, role


def enrich_staff_pg_from_role(user: dict, features: list[str], role: dict | None) -> dict:
    """Attach live PG ACL fields onto a staff dict (always replaces cookie ACL)."""
    out = dict(user)
    out["pg_permissions"] = list(features or [])
    # Always overwrite — never keep stale cookie pg_* after role removal.
    out["pg_writes"] = map_pg_role_writes(role)
    out["pg_actions"] = map_pg_role_actions(role)
    out["pg_user_actions"] = role_user_actions(role)
    out["pg_access"] = role_access_limits(role)
    return out


def staff_pg_writes(staff: dict) -> dict[str, bool]:
    if staff.get("role") == "admin":
        return {k: True for k in ("users", "templates", "groups", "hosts", "nodes")}
    raw = staff.get("pg_writes") or {}
    return {
        "users": bool(raw.get("users")),
        "templates": bool(raw.get("templates")),
        "groups": bool(raw.get("groups")),
        "hosts": bool(raw.get("hosts")),
        "nodes": bool(raw.get("nodes")),
    }


def staff_pg_action(staff: dict, resource: str, action: str) -> bool:
    """True when staff may perform the exact PG action on a resource."""
    if staff.get("role") == "admin":
        return True
    matrix = staff.get("pg_actions") or {}
    block = matrix.get(resource) if isinstance(matrix, dict) else None
    if isinstance(block, dict) and action in block:
        return bool(block.get(action))
    # Legacy cookies without pg_actions: fail closed for mutations
    return False


def staff_user_actions(staff: dict) -> dict[str, bool]:
    if staff.get("role") == "admin":
        return {
            "create": True,
            "read": True,
            "update": True,
            "delete": True,
            "reset_usage": True,
            "revoke_sub": True,
            "disable": True,
            "enable": True,
        }
    raw = staff.get("pg_user_actions") or {}
    return {
        "create": bool(raw.get("create")),
        "read": bool(raw.get("read")),
        "update": bool(raw.get("update")),
        "delete": bool(raw.get("delete")),
        "reset_usage": bool(raw.get("reset_usage")),
        "revoke_sub": bool(raw.get("revoke_sub")),
        "disable": bool(raw.get("disable") or raw.get("update")),
        "enable": bool(raw.get("enable") or raw.get("update")),
    }
