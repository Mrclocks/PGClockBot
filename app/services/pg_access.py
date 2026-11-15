from __future__ import annotations

"""Map PasarGuard admin-role permissions → lightweight web-panel feature keys.

Resellers never get PG settings / admin_roles / cores / api_keys in our UI.

Hybrid Owner ACL: platform web Owner keeps full *shop* power, but PasarGuard
menus/actions are clamped to the env ``PG_USERNAME`` role (fail-closed).
"""

import asyncio
from typing import Any, Mapping

from app.services.pasarguard import PasarGuardError, is_panel_outage
from app.services.redact import user_safe_error
from app.services.security_policy import UnsafePgUrlError, assert_safe_pg_base_url
import logging

logger = logging.getLogger(__name__)

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

# Owner-only PG UI (admin management) — never granted from limited role maps.
PG_OWNER_ONLY_FEATURES = ("pg_admins",)

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

PG_API_KEY_REJECTED_MSG = (
    "کلید API پاسارگارد پذیرفته نشد. "
    "علت محتمل: کلید اشتباه، منقضی یا باطل شده، یا به سقف درخواست رسیده است. "
    "راه حل: در پاسارگارد یک کلید API فعال بسازید و مقدار کامل آن را وارد کنید، "
    "یا کلید API را حذف کنید تا ورود با رمز انجام شود."
)
PG_ADMIN_DISABLED_MSG = (
    "حساب این ادمین در پاسارگارد غیرفعال است. "
    "راه حل: ادمین را در پاسارگارد فعال کنید یا اعتبارنامه ادمین دیگری وارد کنید."
)


def clear_platform_pg_capability_cache() -> None:
    """Drop cached PG ACL probes (call after setup / PG credential / permission changes)."""
    from app.services.pg_read_cache import invalidate_capability_cache

    invalidate_capability_cache()


def _capability_ident(*, base_url: str, username: str, partition: str = "platform") -> str:
    """Partition capability answers by panel identity; never share across shops."""
    user = (username or "").strip().lower()
    base = (base_url or "").rstrip("/")
    if user:
        return f"u:{user}@{base}:{partition}"
    return f"owner@{base}:{partition}"


def _client_capability_ident(pg: Any) -> str:
    """Prefer the client's own cache partition; never await / share across shops."""
    import inspect

    bound = getattr(pg, "_read_cache_ident", None)
    if callable(bound):
        try:
            value = bound()
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
        if inspect.isawaitable(value):
            # AsyncMock or similar — ignore and fall back to attributes.
            try:
                value.close()  # type: ignore[attr-defined]
            except Exception:
                pass
    uname = str(getattr(pg, "_login_username", "") or "").strip()
    base = str(getattr(pg, "base_url", "") or "")
    partition = str(getattr(pg, "_cache_partition", "role") or "role")
    return _capability_ident(base_url=base, username=uname, partition=partition)


def _nested_role(admin: dict | None) -> dict | None:
    if not isinstance(admin, dict):
        return None
    nested = admin.get("role")
    return nested if isinstance(nested, dict) else None


def _role_is_present(role: dict | None) -> bool:
    if not isinstance(role, dict):
        return False
    return bool(
        role.get("id") is not None
        or role.get("name")
        or role.get("permissions") is not None
        or "is_owner" in role
    )


def _admin_looks_like_pg_owner(admin: dict | None, role: dict | None) -> bool:
    """True when PasarGuard marks this account as panel owner.

    PasarGuard itself uses ``role.is_owner`` (not leftover ``is_sudo``).
    Legacy sudo/superuser only counts when no role object exists.
    """
    effective = role if _role_is_present(role) else _nested_role(admin)
    if _role_is_present(effective):
        return bool(effective.get("is_owner"))
    if not isinstance(admin, dict):
        return False
    return bool(admin.get("is_owner") or admin.get("is_sudo") or admin.get("is_superuser"))


def acl_from_admin_payload(admin: dict | None) -> tuple[list[str], dict | None, bool]:
    """Map a live ``GET /api/admin`` payload to (features, role, pg_is_owner).

    Nested ``role.permissions`` is the same matrix the PasarGuard panel uses
    for this signed-in admin — quotas already read this object every page.
    """
    role = _nested_role(admin)
    pg_is_owner = _admin_looks_like_pg_owner(admin, role)
    if pg_is_owner:
        out_role = dict(role) if isinstance(role, dict) else {"is_owner": True}
        out_role["is_owner"] = True
        return full_pg_owner_features(), out_role, True
    features = map_pg_role_to_features(role) if role else []
    return list(features or []), role, False


async def resolve_acl_from_client(
    client: Any, *, username: str | None = None
) -> tuple[list[str], dict | None, dict | None]:
    """Live ACL for the token holder: current admin nested role, then role-id fallback."""
    admin: dict | None = None
    try:
        current = await client.get_current_admin()
        if isinstance(current, dict) and current:
            admin = current
    except Exception:
        admin = None
    if not isinstance(admin, dict):
        admin = None
    features, role, _owner = acl_from_admin_payload(admin)
    if _role_is_present(role):
        return list(features or []), role, admin
    uname = (username or "").strip()
    if not uname and isinstance(admin, dict):
        uname = str(admin.get("username") or "").strip()
    role_id = None
    if isinstance(admin, dict):
        for key in ("role_id", "admin_role_id"):
            if admin.get(key) is not None:
                try:
                    role_id = int(admin[key])
                    break
                except (TypeError, ValueError):
                    pass
        nested = _nested_role(admin)
        if role_id is None and isinstance(nested, dict) and nested.get("id") is not None:
            try:
                role_id = int(nested["id"])
            except (TypeError, ValueError):
                role_id = None
    if role_id is None and uname:
        try:
            from app.services.pg_staff_access import resolve_pg_role_id_for_admin

            live = await resolve_pg_role_id_for_admin(uname, client=client)
            if live:
                role_id = int(live)
        except Exception:
            role_id = None
    if role_id:
        features, role = await resolve_reseller_pg_features(role_id, client=client)
        if _admin_looks_like_pg_owner(admin, role):
            features = full_pg_owner_features()
            if isinstance(role, dict):
                role = dict(role)
                role["is_owner"] = True
            else:
                role = {"is_owner": True, "id": role_id}
    return list(features or []), role, admin


def full_pg_owner_features() -> list[str]:
    """Complete PG sidebar keys for a true PasarGuard owner account."""
    out = list(PG_FEATURE_KEYS)
    for k in PG_OWNER_ONLY_FEATURES:
        if k not in out:
            out.append(k)
    return out


class _CredentialsRejected(Exception):
    """The panel answered that the probed credentials cannot be used."""


# PasarGuard's exact answer for a disabled admin; any other 403 is a role refusal.
PG_ACCOUNT_DISABLED_DETAIL = "your account has been disabled"


def _says_account_disabled(exc: PasarGuardError) -> bool:
    return (exc.detail() or "").strip().lower() == PG_ACCOUNT_DISABLED_DETAIL


def _rejected_credentials_reason(exc: Exception, *, uses_api_key: bool) -> str | None:
    """Persian reason when the self lookup proves the login unusable, else ``None``.

    A 403 or 404 from a limited role or an older panel is not proof: the
    caller keeps its fallback lookups for those.
    """
    if not isinstance(exc, PasarGuardError):
        return None
    if exc.status_code == 401:
        return PG_API_KEY_REJECTED_MSG if uses_api_key else exc.user_message()
    if exc.status_code == 403 and _says_account_disabled(exc):
        return PG_ADMIN_DISABLED_MSG
    return None


async def _read_probe_admin(client: Any, uname: str, *, uses_api_key: bool) -> dict:
    """The signed-in admin's own record for the capability probe.

    ``GET /api/admin`` needs no ``admins.read``, so it comes first. Directory
    lookups often 403 for roles that only have ``users.create``, and a
    username stub keeps a valid login from reading as "admin not found".
    An API key is never checked when the client is built, so this is the
    first request that can reject it: a refusal or an unreachable panel
    stops the probe instead of falling through to the stub.
    """
    try:
        current = await client.get_current_admin()
    except Exception as exc:
        if is_panel_outage(exc):
            raise
        reason = _rejected_credentials_reason(exc, uses_api_key=uses_api_key)
        if reason:
            raise _CredentialsRejected(reason) from exc
        current = None
    if isinstance(current, dict) and current:
        return current
    admin = await client.get_admin(uname)
    if isinstance(admin, dict):
        return admin
    return {"username": uname}


async def _role_is_owner_role(client: Any, role_id: int) -> bool | None:
    """Whether the PasarGuard role with this id is the owner role; ``None`` if unreadable.

    Report-only: a failure here must never turn a valid login into a failed setup.
    """
    try:
        roles = await client.get_admin_roles()
    except Exception:
        logger.debug("admin role list unavailable for the setup report", exc_info=True)
        return None
    match = next(
        (r for r in roles if isinstance(r, dict) and str(r.get("id")) == str(role_id)),
        None,
    )
    if match is None or "is_owner" not in match:
        return None
    return bool(match.get("is_owner"))


async def _auth_method_fields(
    client: Any, *, uses_api_key: bool, inspect_key: bool, role_id: int | None
) -> dict[str, Any]:
    """How the probe signed in, for the setup report.

    PasarGuard answers a key created without "inherit permissions" with
    ``is_owner=False`` and only the key's own permissions, even when the
    admin holds the owner role. ``api_key_overrides_owner_role`` is True when
    the role list shows exactly that, and ``None`` when it cannot be read.
    Only the setup and connection-test probes look, to keep the cached
    request-path probe to one panel call.
    """
    fields: dict[str, Any] = {"auth_method": "api_key" if uses_api_key else "password"}
    if uses_api_key and inspect_key and role_id is not None:
        fields["api_key_overrides_owner_role"] = await _role_is_owner_role(client, role_id)
    return fields


async def resolve_platform_pg_capabilities(
    *,
    username: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Resolve PG menu/action ACL for platform env credentials (Hybrid Owner).

    Fail-closed: any probe/role failure → empty features (no PG UI).
    Does not elevate beyond the token of the given / env credentials.
    Auth: API key (X-Api-Key) preferred over password when either is provided.
    """
    from app.config import get_settings
    from app.services.pasarguard import PasarGuardClient, PasarGuardError, new_panel_http_client

    from app.services.pg_read_cache import (
        CAPABILITY_PROBE_MAX_AGE_SEC,
        CAPABILITY_PROBE_PATH,
        cache_get,
        cache_put,
    )

    settings = get_settings()
    env_username = getattr(settings, "pg_username", None)
    env_password = getattr(settings, "pg_password", None)
    env_api_key = getattr(settings, "pg_api_key", None)
    env_base_url = getattr(settings, "pg_base_url", None)
    uname = (username if username is not None else env_username or "").strip()
    pwd = (password if password is not None else (env_password or "")).replace(
        "\r", ""
    ).strip()
    key = (api_key if api_key is not None else (env_api_key or "")).replace(
        "\r", ""
    ).strip()
    # base_url override only for setup probe (temporary client)
    own_client = bool(
        username is not None
        or password is not None
        or api_key is not None
        or base_url is not None
    )
    cache_ident = _capability_ident(
        base_url=str(base_url or env_base_url or ""),
        username=uname,
        partition="platform",
    )
    if use_cache and not own_client:
        hit = cache_get(
            cache_ident,
            CAPABILITY_PROBE_PATH,
            None,
            max_age=CAPABILITY_PROBE_MAX_AGE_SEC,
        )
        if isinstance(hit, dict) and hit.get("ok") is True:
            return dict(hit)

    empty: dict[str, Any] = {
        "ok": False,
        "error": None,
        "username": uname or None,
        "pg_is_owner": False,
        "pg_role_id": None,
        "features": [],
        "role": None,
        "admin": None,
    }
    if not uname or not (pwd or key):
        empty["error"] = "اعتبارنامه پاسارگارد ناقص است (نام کاربری + رمز یا کلید API)"
        return empty

    client: PasarGuardClient | None = None
    try:
        if own_client:
            # Temporary client for setup probe — do not touch global get_pg() singleton.
            # Prefer API key when provided; otherwise password grant.
            client = PasarGuardClient(
                username=uname,
                password=pwd or None,
                api_key=key or None,
            )
            if base_url:
                try:
                    client.base_url = assert_safe_pg_base_url(str(base_url).rstrip("/"), resolve_dns=False)
                except UnsafePgUrlError as exc:
                    empty["error"] = str(exc)
                    return empty
                # Rebind the temporary client to the probed base before any request.
                old = client._client
                client._client = new_panel_http_client(client.base_url)
                try:
                    await old.aclose()
                except Exception:
                    logger.debug("resolve_platform_pg_capabilities: ignored failure", exc_info=True)
        else:
            from app.services.pasarguard import get_pg

            client = get_pg()

        await client.ensure_token()
        admin = await _read_probe_admin(client, uname, uses_api_key=bool(key))
        got = str(admin.get("username") or "").strip()
        if got:
            uname = got

        role_id = None
        nested_role = admin.get("role")
        if isinstance(nested_role, dict) and nested_role.get("id") is not None:
            try:
                role_id = int(nested_role["id"])
            except (TypeError, ValueError):
                role_id = None
        if role_id is None:
            for key in ("role_id", "admin_role_id"):
                if admin.get(key) is not None:
                    try:
                        role_id = int(admin[key])
                        break
                    except (TypeError, ValueError):
                        pass

        features, role, pg_is_owner = acl_from_admin_payload(admin)
        # Nested role on GET /api/admin is live (same source as quotas).
        # GET /admin-roles/{id} is a fallback only — it was cached separately
        # and kept revoked node/host menus visible after PasarGuard edits.
        if not _role_is_present(role) and role_id is not None:
            features, role = await resolve_reseller_pg_features(
                role_id, client=client
            )
            pg_is_owner = _admin_looks_like_pg_owner(admin, role)
            if pg_is_owner:
                features = full_pg_owner_features()
                if not role:
                    role = {"is_owner": True, "permissions": {}}
                else:
                    role = dict(role)
                    role["is_owner"] = True
            elif role:
                features = map_pg_role_to_features(role)

        payload = {
            "ok": True,
            "error": None,
            "username": uname,
            "pg_is_owner": pg_is_owner,
            "pg_role_id": role_id,
            "features": list(features or []),
            "role": role,
            "admin": admin,
        }
        payload.update(
            await _auth_method_fields(
                client,
                uses_api_key=bool(key),
                inspect_key=own_client and not pg_is_owner,
                role_id=role_id,
            )
        )
        # Cache only successful probes; failures stay fail-closed without a long TTL.
        if use_cache and not own_client and payload["ok"]:
            cache_put(cache_ident, CAPABILITY_PROBE_PATH, None, dict(payload))
        return payload
    except _CredentialsRejected as e:
        empty["error"] = str(e)
        return empty
    except PasarGuardError as e:
        empty["error"] = e.user_message(fallback=str(e))
        return empty
    except Exception as e:
        # Transport errors carry internal hosts and ports; show a mapped Persian cause only.
        logger.warning("PasarGuard capability probe failed", exc_info=True)
        empty["error"] = user_safe_error(e, fallback="خطا در خواندن نقش پاسارگارد")
        return empty
    finally:
        if own_client and client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("resolve_platform_pg_capabilities: ignored failure", exc_info=True)


PG_CHECK_TIMEOUT_SEC = 8.0
PG_CHECK_TIMEOUT_MSG = "پنل در زمان مقرر پاسخ نداد؛ آدرس و اتصال پنل را بررسی کنید"


async def check_pg_credentials(
    *, username: str, password: str, api_key: str, base_url: str
) -> str | None:
    """Sign in with these PasarGuard credentials; a safe Persian reason on failure, else ``None``.

    Used before credentials are saved from settings, so a wrong password or a
    refused API key is reported instead of being written to ``.env``.
    """
    try:
        caps = await asyncio.wait_for(
            resolve_platform_pg_capabilities(
                username=username,
                password=password or None,
                api_key=api_key or None,
                base_url=base_url,
                use_cache=False,
            ),
            timeout=PG_CHECK_TIMEOUT_SEC,
        )
    except TimeoutError:
        return PG_CHECK_TIMEOUT_MSG
    if caps.get("ok"):
        return None
    detail = caps.get("error") or "اعتبارنامه پذیرفته نشد"
    return f"اتصال پاسارگارد ناموفق بود: {detail}"


async def enrich_platform_admin_staff(user: dict) -> dict:
    """Attach live PG ACL onto a web Owner / platform-admin staff dict."""
    import logging

    try:
        caps = await resolve_platform_pg_capabilities()
    except Exception:
        logging.getLogger(__name__).exception("enrich_platform_admin_staff probe failed")
        # Fail closed: shop Owner keeps panel access; PG menus stay empty.
        out = enrich_staff_pg_from_role(dict(user), [], None)
        out["pg_is_owner"] = False
        out["pg_capabilities_ok"] = False
        out["pg_capabilities_error"] = "بررسی دسترسی پاسارگارد ناموفق"
        return out
    features = list(caps.get("features") or [])
    role = caps.get("role") if isinstance(caps.get("role"), dict) else None
    out = enrich_staff_pg_from_role(user, features, role)
    out["pg_is_owner"] = bool(caps.get("pg_is_owner"))
    out["pg_capabilities_ok"] = bool(caps.get("ok"))
    out["pg_capabilities_error"] = caps.get("error")
    if caps.get("username"):
        out["pg_admin_username"] = caps["username"]
    if caps.get("pg_role_id") is not None:
        out["pg_role_id"] = caps["pg_role_id"]
    # Owner-equivalent: ensure action matrices are fully open
    if out.get("pg_is_owner"):
        out["pg_permissions"] = full_pg_owner_features()
        out["pg_actions"] = map_pg_role_actions({"is_owner": True})
        out["pg_user_actions"] = role_user_actions({"is_owner": True})
        out["pg_writes"] = map_pg_role_writes({"is_owner": True})
    return out


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
    # Own-account overview (quota/users) mirrors native PasarGuard home for limited
    # roles that have resource access but lack system.read.
    if out and "pg_overview" not in out:
        out.insert(0, "pg_overview")
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
    """Exact PasarGuard action matrix per resource (create/update/delete/reconnect/…)."""
    resources = {
        "users": ("create", "update", "delete"),
        "templates": ("create", "update", "delete"),
        "groups": ("create", "update", "delete"),
        "hosts": ("create", "update", "delete"),
        "nodes": ("create", "update", "delete", "reconnect", "stats"),
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


async def resolve_reseller_pg_features(
    pg_role_id: int | None,
    *,
    client: Any | None = None,
) -> tuple[list[str], dict | None]:
    """Fetch role from PasarGuard and return (feature_keys, raw_role).

    Cached ~3 minutes per client identity via ``pg_read_cache`` so two shops never
    share an answer. ``client`` must be the caller-allowed PG client
    (Principal/parent/reseller/staff). Omitting ``client`` uses Owner ``get_pg()``.
    """
    from app.services.pg_read_cache import (
        CAPABILITY_PROBE_MAX_AGE_SEC,
        ROLE_FEATURES_PATH,
        cache_get,
        cache_put,
    )

    if not pg_role_id:
        return [], None
    rid = int(pg_role_id)

    pg = client
    if pg is None:
        from app.services.pasarguard import get_pg

        pg = get_pg()

    ident = _client_capability_ident(pg)
    params = {"role_id": rid}
    cached = cache_get(ident, ROLE_FEATURES_PATH, params, max_age=CAPABILITY_PROBE_MAX_AGE_SEC)
    if isinstance(cached, dict) and cached.get("ok") is True:
        features = list(cached.get("features") or [])
        role = cached.get("role")
        return features, dict(role) if isinstance(role, dict) else None

    role = None
    try:
        role = await pg.get_admin_role(rid)
    except Exception:
        # Fallback: try list and find by id
        try:
            roles = await pg.get_admin_roles()
            role = next((r for r in roles if int(r.get("id") or 0) == rid), None)
        except Exception:
            return [], None
    if not isinstance(role, dict):
        return [], None
    features = map_pg_role_to_features(role)
    cache_put(
        ident,
        ROLE_FEATURES_PATH,
        params,
        {"ok": True, "features": list(features), "role": dict(role)},
    )
    return features, role


def staff_from_platform_caps(caps: Mapping[str, Any] | None) -> dict[str, Any]:
    """Staff-shaped dict from ``resolve_platform_pg_capabilities`` (no secrets).

    Used by the setup audit, Hybrid Owner menus, and bot quota/rep gates so
    every surface derives ACL from the same probe payload.
    """
    caps = caps or {}
    role = caps.get("role") if isinstance(caps.get("role"), dict) else None
    features = list(caps.get("features") or [])
    pg_is_owner = bool(caps.get("pg_is_owner"))
    if pg_is_owner:
        features = full_pg_owner_features()
        role = dict(role or {})
        role["is_owner"] = True
    staff = enrich_staff_pg_from_role(
        {"role": "admin", "pg_is_owner": pg_is_owner},
        features,
        role,
    )
    staff["pg_is_owner"] = pg_is_owner
    if caps.get("username"):
        staff["pg_admin_username"] = caps["username"]
    if caps.get("pg_role_id") is not None:
        staff["pg_role_id"] = caps["pg_role_id"]
    return staff


PLAN_CATALOG_FEATURES = ("pg_templates", "pg_groups")

AUDIT_NOTE_CUSTOM_KEY_ON_OWNER = (
    "این کلید API با «دسترسی سفارشی» ساخته شده است. ادمین در پاسارگارد owner است، "
    "ولی پاسارگارد چنین کلیدی را هیچ‌وقت owner حساب نمی‌کند و فقط دسترسی‌های خود کلید را می‌پذیرد. "
    "راه حل: در پاسارگارد برای این کلید «ارث‌بری دسترسی‌ها» را روشن کنید یا با رمز وصل شوید."
)
AUDIT_NOTE_API_KEY_LIMITED = (
    "اتصال با کلید API است. اگر این ادمین در پاسارگارد owner است ولی اینجا حساب محدود نشان داده می‌شود، "
    "کلید با «دسترسی سفارشی» ساخته شده است؛ برای این کلید «ارث‌بری دسترسی‌ها» را روشن کنید یا با رمز وصل شوید."
)


def _missing_catalog_note(feature_keys: list[str]) -> str | None:
    missing = [PG_FEATURE_LABELS[k] for k in PLAN_CATALOG_FEATURES if k not in feature_keys]
    if not missing:
        return None
    names = " و ".join(f"«{label}»" for label in missing)
    return (
        f"این حساب به {names} پاسارگارد دسترسی ندارد، پس در ساخت پلن قابل انتخاب نیستند. "
        f"راه حل: در نقش این ادمین (یا دسترسی کلید API) خواندن {names} را فعال کنید."
    )


def _audit_notes(
    caps: Mapping[str, Any], *, feature_keys: list[str], pg_is_owner: bool
) -> list[str]:
    """Plain-language warnings for the setup report; an owner account needs none."""
    if pg_is_owner:
        return []
    notes: list[str] = []
    if caps.get("auth_method") == "api_key":
        # True: confirmed owner role behind a custom key. None: role list unreadable,
        # so explain the possibility. False: a genuinely limited role, nothing to add.
        overrides_owner = caps.get("api_key_overrides_owner_role")
        if overrides_owner is True:
            notes.append(AUDIT_NOTE_CUSTOM_KEY_ON_OWNER)
        elif overrides_owner is None:
            notes.append(AUDIT_NOTE_API_KEY_LIMITED)
    catalog_note = _missing_catalog_note(feature_keys)
    if catalog_note:
        notes.append(catalog_note)
    return notes


def public_pg_access_audit(caps: Mapping[str, Any] | None) -> dict[str, Any]:
    """Display-only snapshot of PG ACL. Never an authorization source."""
    from app.services.formatting import format_bytes
    from app.services.pg_quota import merge_role_limits

    caps = dict(caps or {})
    username = caps.get("username")
    empty: dict[str, Any] = {
        "ok": False,
        "error": caps.get("error") or "بررسی دسترسی پاسارگارد ناموفق بود",
        "pg_is_owner": False,
        "username": username,
        "role_name": None,
        "account_label": "نامشخص",
        "features": [],
        "can_create_admin": False,
        "can_manage_representatives": False,
        "shop_full": True,
        "limits": [],
        "notes": [],
    }
    if not caps.get("ok"):
        return empty

    staff = staff_from_platform_caps(caps)
    pg_is_owner = bool(staff.get("pg_is_owner"))
    can_create = pg_is_owner or staff_has_pg_admins_create(staff)
    role = staff.get("pg_role") if isinstance(staff.get("pg_role"), dict) else None
    admin = caps.get("admin") if isinstance(caps.get("admin"), dict) else None
    role_name = None
    if isinstance(role, dict):
        raw_name = role.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            role_name = raw_name.strip()
    feature_keys = list(staff.get("pg_permissions") or [])
    features = [
        {"key": k, "label": PG_FEATURE_LABELS.get(k, k)}
        for k in feature_keys
        if k in PG_FEATURE_LABELS
    ]
    limits_rows: list[dict[str, str]] = []
    if pg_is_owner:
        limits_rows.append({"label": "سقف نقش", "value": "بدون محدودیت (مالک پاسارگارد)"})
    else:
        merged = merge_role_limits(admin, role)

        def _pos(raw: Any) -> int | None:
            if raw is None or raw == "":
                return None
            try:
                n = int(float(raw))
            except (TypeError, ValueError):
                return None
            return n if n > 0 else None

        max_users = _pos(merged.get("max_users"))
        users_now = _pos((admin or {}).get("total_users")) or _pos(
            (admin or {}).get("users_count")
        )
        if max_users is not None:
            used = f"{users_now} از " if users_now is not None else ""
            limits_rows.append({"label": "سقف کاربران", "value": f"{used}{max_users}"})
        elif users_now is not None:
            limits_rows.append({"label": "کاربران فعلی", "value": str(users_now)})
        else:
            limits_rows.append({"label": "سقف کاربران", "value": "بدون سقف نقش"})

        acct_cap = _pos((admin or {}).get("data_limit"))
        if acct_cap:
            used_traffic = _pos((admin or {}).get("used_traffic")) or _pos(
                (admin or {}).get("traffic_used")
            )
            label = format_bytes(acct_cap)
            if used_traffic:
                label = f"{format_bytes(used_traffic)} از {label}"
            limits_rows.append({"label": "سقف حجم حساب", "value": label})
        per_user = _pos(merged.get("data_limit_max"))
        if per_user:
            limits_rows.append(
                {"label": "حداکثر حجم هر کاربر", "value": format_bytes(per_user)}
            )

    return {
        "ok": True,
        "error": None,
        "pg_is_owner": pg_is_owner,
        "username": username,
        "role_name": role_name,
        "account_label": (
            "مالک کامل پاسارگارد" if pg_is_owner else "حساب محدود پاسارگارد"
        ),
        "features": features,
        "can_create_admin": can_create,
        "can_manage_representatives": can_create,
        "shop_full": True,
        "limits": limits_rows,
        "notes": _audit_notes(caps, feature_keys=feature_keys, pg_is_owner=pg_is_owner),
    }


def staff_has_pg_admins_create(staff: Mapping[str, Any] | None) -> bool:
    """True only for actual PasarGuard ``admins.create`` — not page visibility."""
    if not staff:
        return False
    actions = staff.get("pg_actions")
    if isinstance(actions, Mapping):
        for key in ("admins", "admin"):
            block = actions.get(key)
            if isinstance(block, Mapping) and _action_allowed(block.get("create")):
                return True
    role = staff.get("pg_role")
    if isinstance(role, Mapping):
        raw = role.get("permissions")
        if isinstance(raw, Mapping):
            for key in ("admins", "admin"):
                block = raw.get(key)
                if isinstance(block, Mapping) and _action_allowed(block.get("create")):
                    return True
    return False


def enrich_staff_pg_from_role(user: dict, features: list[str], role: dict | None) -> dict:
    """Attach live PG ACL fields onto a staff dict (mutates a copy)."""
    out = dict(user)
    out["pg_permissions"] = list(features or [])
    if role:
        # Keep raw role so ``admins.create`` can be checked without treating
        # ``pg_admins`` page visibility as authority.
        out["pg_role"] = dict(role)
        out["pg_writes"] = map_pg_role_writes(role)
        out["pg_actions"] = map_pg_role_actions(role)
        out["pg_user_actions"] = role_user_actions(role)
        out["pg_access"] = role_access_limits(role)
        name = role.get("name")
        if isinstance(name, str) and name.strip():
            out["pg_role_name"] = name.strip()
        rid = role.get("id")
        if rid is not None and out.get("pg_role_id") is None:
            try:
                out["pg_role_id"] = int(rid)
            except (TypeError, ValueError):
                pass
    return out


def staff_pg_writes(staff: dict) -> dict[str, bool]:
    """Broad write flags — Hybrid: Owner shop bypass does not imply PG writes."""
    from app.services.authz import authz_from_staff, can_pg_write_resource

    ctx = authz_from_staff(staff)
    keys = ("users", "templates", "groups", "hosts", "nodes")
    return {k: can_pg_write_resource(ctx, k) for k in keys}


def staff_pg_action(staff: dict, resource: str, action: str) -> bool:
    """True when staff may perform the exact PG action on a resource.

    Phase 2C/3D: Level-1 and Level-2 Principals also pass local-safety
    (never Owner-grade ops) and fail closed when PG identity/capabilities
    are not ready.
    """
    from app.services.authz import authz_from_staff, can_pg_action
    from app.services.principal_pg_authz import (
        is_level1_principal_staff,
        local_safety_allows_pg_action,
        principal_pg_authz_ready,
    )

    if is_level1_principal_staff(staff) and not principal_pg_authz_ready(staff):
        return False
    if is_level1_principal_staff(staff) and not local_safety_allows_pg_action(
        staff, resource, action
    ):
        return False
    return can_pg_action(authz_from_staff(staff), resource, action)


def staff_user_actions(staff: dict) -> dict[str, bool]:
    from app.services.authz import authz_from_staff, can_pg_user_action
    from app.services.principal_pg_authz import (
        is_level1_principal_staff,
        local_safety_allows_pg_action,
    )

    ctx = authz_from_staff(staff)
    keys = (
        "create",
        "read",
        "update",
        "delete",
        "reset_usage",
        "revoke_sub",
        "disable",
        "enable",
    )
    out = {k: can_pg_user_action(ctx, k) for k in keys}
    if is_level1_principal_staff(staff):
        for k in list(out.keys()):
            if out[k] and not local_safety_allows_pg_action(staff, "users", k):
                out[k] = False
    return out
