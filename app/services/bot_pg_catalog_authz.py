"""Bot PG template/group authorization (shared with Web Authz).

Telegram identity → OrgPrincipal → AuthzContext → live PG capability
→ existing plans_catalog allow-list proof → local safety → action.

Do not invent a second allow-list. Owner keeps documented unrestricted
behavior (``pg_is_owner``). Non-Owner missing / invalid / unresolved
allow-list → DENY.

Callback/message data may carry a resource id only. Hierarchy / PG identity
fields are ignored and treated as tamper → DENY.

Shop bots are isolated. Bound L2 uses the same template/group gate as L1
with the existing allow-list and ``get_pg_for_principal(L2.id)``.

Bot UI for templates/groups is the existing hint family plus user-create
pickers. Mutation authorization uses the same gate as Web (no new RBAC).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.authz import AuthDecision
from app.services.bot_principal_identity import (
    BotPrincipalResolution,
    bot_pg_client_for_resolution,
    callback_carries_principal_tamper,
    resolve_bot_pg_family_identity,
)
from app.services.plans_catalog import (
    _admin_pg_unrestricted,
    _allowed_id_set,
    filter_groups_for_staff,
    filter_templates_for_staff,
    groups_allowed_for_staff,
    template_allowed_for_staff,
)
from app.services.principal_pg_authz import (
    apply_level1_pg_local_safety,
    authorize_pg_action,
    authorize_pg_page,
    is_level1_principal_staff,
    principal_pg_authz_ready,
)

BotPgCatalogKind = Literal["templates", "groups"]
BotPgCatalogAction = Literal["list", "read", "create", "update", "delete"]

_COLLECTION_ACTIONS = frozenset({"list", "create"})
_PAGE_KEY = {"templates": "pg_templates", "groups": "pg_groups"}
_PG_RESOURCE = {"templates": "templates", "groups": "groups"}

_TPL_HINT_RE = re.compile(r"^adm:pg:template$")
_GRP_HINT_RE = re.compile(r"^adm:pg:group$")
_SETTPL_RE = re.compile(r"^adm:pg:settpl:(\d+)$")
_TOGGRP_RE = re.compile(r"^adm:pg:toggrp:(\d+)$")
_EDGRP_RE = re.compile(r"^adm:pg:edgrp:(\d+)$")

_ACTION_ID_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "read": (_SETTPL_RE,),
    "update": (_TOGGRP_RE, _EDGRP_RE),
}

_TAMPER_NEEDLES = (
    "pg_username",
    "pg_admin",
    "parent_id",
    "org_scope",
    "org_visible",
    "web_owner",
    "org_depth",
    "org_principal",
    "org_parent",
    "principal_id",
    "depth=",
)

_CLIENT_OWNER_KEYS = (
    "admin",
    "owner",
    "owner_username",
    "admin_username",
    "org_principal_id",
    "parent_id",
    "org_parent_id",
    "org_depth",
    "web_owner",
    "org_scope",
    "principal_id",
    "pg_username",
)

_USER_MESSAGES = {
    "unauthenticated": "ادمین نیستید",
    "shop_bot_isolated": "ادمین نیستید",
    "identity_tamper": "اجازه این عمل را ندارید",
    "inactive_or_missing_principal": "ادمین نیستید",
    "missing_principal": "ادمین نیستید",
    "pg_permission_denied": "اجازه این عمل را ندارید",
    "pg_capabilities_unavailable": "اجازه این عمل را ندارید",
    "resource_out_of_scope": "اجازه این عمل را ندارید",
    "resource_not_found": "اجازه این عمل را ندارید",
    "pg_outage": "اجازه این عمل را ندارید",
    "local_safety_denied": "اجازه این عمل را ندارید",
    "invalid_resource": "اجازه این عمل را ندارید",
    "missing_scope": "اجازه این عمل را ندارید",
    "unsupported_kind": "اجازه این عمل را ندارید",
}


@dataclass(frozen=True)
class BotPgCatalogGate:
    allowed: bool
    reason: str
    user_message: str
    resolution: BotPrincipalResolution | None = None
    staff: dict[str, Any] | None = None
    pg_object: dict[str, Any] | None = None
    pg_client: Any = None
    kind: str = ""
    as_owner_client: bool = False


def _deny(
    reason: str,
    *,
    resolution: BotPrincipalResolution | None = None,
    kind: str = "",
) -> BotPgCatalogGate:
    return BotPgCatalogGate(
        allowed=False,
        reason=reason,
        user_message=_USER_MESSAGES.get(reason, "اجازه این عمل را ندارید"),
        resolution=resolution,
        kind=kind,
    )


def callback_carries_identity_tamper(callback_data: str | None) -> bool:
    if callback_carries_principal_tamper(callback_data):
        return True
    if not callback_data:
        return False
    lowered = str(callback_data).lower()
    return any(needle in lowered for needle in _TAMPER_NEEDLES)


def parse_pg_catalog_callback_id(
    callback_data: str | None, *, action: BotPgCatalogAction
) -> int | None:
    if not callback_data:
        return None
    raw = str(callback_data).strip()
    patterns = _ACTION_ID_PATTERNS.get(str(action)) or ()
    for match_re in patterns:
        match = match_re.match(raw)
        if match is None:
            continue
        try:
            oid = int(match.group(1))
        except (TypeError, ValueError):
            return None
        return oid if oid > 0 else None
    return None


def sanitize_pg_catalog_write_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    for key in _CLIENT_OWNER_KEYS:
        out.pop(key, None)
    return out


def catalog_template_allowed(staff: Mapping[str, Any] | None, template_id: int | None) -> bool:
    """Existing template allow-list, fail-closed on unresolved list for non-Owner."""
    if not template_allowed_for_staff(staff, template_id):
        return False
    if _admin_pg_unrestricted(staff):
        return True
    allowed = _allowed_id_set((staff or {}).get("pg_access", {}).get("allowed_template_ids"))
    if allowed is None:
        return False
    return True


def catalog_groups_allowed(staff: Mapping[str, Any] | None, group_ids: list[int] | None) -> bool:
    """Existing group allow-list, fail-closed on unresolved list for non-Owner."""
    ids = [int(g) for g in (group_ids or []) if int(g) > 0]
    if not groups_allowed_for_staff(staff, ids):
        return False
    if _admin_pg_unrestricted(staff):
        return True
    allowed = _allowed_id_set((staff or {}).get("pg_access", {}).get("allowed_group_ids"))
    if allowed is None:
        return False
    return True


def _prepare_staff(resolution: BotPrincipalResolution) -> dict[str, Any] | None:
    staff = dict(resolution.staff)
    if is_level1_principal_staff(staff):
        staff = apply_level1_pg_local_safety(staff)
    if not principal_pg_authz_ready(staff):
        return None
    return staff


def _capability_decision(
    staff: dict[str, Any],
    *,
    kind: BotPgCatalogKind,
    action: BotPgCatalogAction,
) -> AuthDecision:
    page_key = _PAGE_KEY.get(str(kind))
    resource = _PG_RESOURCE.get(str(kind))
    if not page_key or not resource:
        return AuthDecision(False, "unsupported_kind")
    if action in {"list", "read"}:
        return authorize_pg_page(staff, page_key)
    return authorize_pg_action(staff, resource, str(action))


async def _resolve_identity(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None,
    is_reseller_bot: bool,
    reseller_profile_id: int | None,
    reseller_owner_id: int | None,
    callback_data: str | None,
) -> BotPgCatalogGate | BotPrincipalResolution:
    if session is None or db_user is None:
        return _deny("unauthenticated")
    got = await resolve_bot_pg_family_identity(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        identity_tamper=callback_carries_identity_tamper(callback_data),
    )
    if isinstance(got, str):
        return _deny(got)
    return got


def _unwrap_list(raw: Any, kind: str) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        return []
    keys = ("templates", "items", "data") if kind == "templates" else ("groups", "items", "data")
    for key in keys:
        inner = raw.get(key)
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []


def _id_of(obj: Mapping[str, Any] | None) -> int:
    if not isinstance(obj, Mapping):
        return 0
    try:
        return int(obj.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _object_in_scope(staff: Mapping[str, Any], *, kind: str, object_id: int) -> bool:
    if kind == "templates":
        return catalog_template_allowed(staff, object_id)
    return catalog_groups_allowed(staff, [object_id])


async def authorize_bot_pg_catalog_op(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None,
    kind: BotPgCatalogKind,
    action: BotPgCatalogAction,
    callback_data: str | None = None,
    object_id: int | None = None,
    group_ids: list[int] | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> BotPgCatalogGate:
    """Authorize a Bot template/group operation (collection or ID-based)."""
    if kind not in {"templates", "groups"}:
        return _deny("unsupported_kind")

    ident = await _resolve_identity(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback_data=callback_data,
    )
    if isinstance(ident, BotPgCatalogGate):
        return ident
    resolution = ident

    staff = _prepare_staff(resolution)
    if staff is None:
        return _deny("pg_capabilities_unavailable", resolution=resolution, kind=kind)

    cap = _capability_decision(staff, kind=kind, action=action)
    if not cap.allowed:
        return BotPgCatalogGate(
            allowed=False,
            reason=cap.reason or "pg_permission_denied",
            user_message=_USER_MESSAGES.get(
                cap.reason, _USER_MESSAGES["pg_permission_denied"]
            ),
            resolution=resolution,
            staff=staff,
            kind=kind,
        )

    try:
        pg_client, as_owner = await bot_pg_client_for_resolution(session, resolution)
    except Exception:
        return _deny("pg_outage", resolution=resolution, kind=kind)

    oid = object_id
    if oid is None and action not in _COLLECTION_ACTIONS:
        oid = parse_pg_catalog_callback_id(callback_data, action=action)

    collection = action in _COLLECTION_ACTIONS
    if action == "list" and callback_data not in {None, "", "adm:pg:template", "adm:pg:group"}:
        return _deny("invalid_resource", resolution=resolution, kind=kind)

    if collection:
        if kind == "templates" and action == "create" and group_ids is not None:
            if not catalog_groups_allowed(staff, group_ids):
                return _deny("resource_out_of_scope", resolution=resolution, kind=kind)
        if action in {"create", "update", "delete"}:
            try:
                from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users

                await assert_can_mutate_owned_users(staff, session=session, client=pg_client)
            except PgQuotaError:
                return _deny("local_safety_denied", resolution=resolution, kind=kind)
            except Exception:
                return _deny("pg_outage", resolution=resolution, kind=kind)
        return BotPgCatalogGate(
            allowed=True,
            reason="ok",
            user_message="",
            resolution=resolution,
            staff=staff,
            pg_client=pg_client,
            kind=kind,
            as_owner_client=bool(as_owner),
        )

    try:
        oid_i = int(oid) if oid is not None else 0
    except (TypeError, ValueError):
        oid_i = 0
    if oid_i <= 0:
        return _deny("invalid_resource", resolution=resolution, kind=kind)

    getter_name = "get_user_template" if kind == "templates" else "get_group"
    getter = getattr(pg_client, getter_name, None)
    if getter is None:
        return _deny("pg_outage", resolution=resolution, kind=kind)
    try:
        raw = await getter(oid_i)
    except Exception:
        return _deny("pg_outage", resolution=resolution, kind=kind)
    if not isinstance(raw, dict) or _id_of(raw) != oid_i:
        return _deny("resource_not_found", resolution=resolution, kind=kind)

    if not _object_in_scope(staff, kind=kind, object_id=oid_i):
        return _deny("resource_out_of_scope", resolution=resolution, kind=kind)

    if kind == "templates" and action in {"create", "update"} and group_ids is not None:
        if not catalog_groups_allowed(staff, group_ids):
            return _deny("resource_out_of_scope", resolution=resolution, kind=kind)

    if action in {"update", "delete"}:
        try:
            from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users

            await assert_can_mutate_owned_users(staff, session=session, client=pg_client)
        except PgQuotaError:
            return _deny("local_safety_denied", resolution=resolution, kind=kind)
        except Exception:
            return _deny("pg_outage", resolution=resolution, kind=kind)

    return BotPgCatalogGate(
        allowed=True,
        reason="ok",
        user_message="",
        resolution=resolution,
        staff=staff,
        pg_object=raw,
        pg_client=pg_client,
        kind=kind,
        as_owner_client=bool(as_owner),
    )


async def list_scoped_pg_catalog(
    gate: BotPgCatalogGate,
    *,
    kind: BotPgCatalogKind | None = None,
) -> list[dict[str, Any]]:
    """Fetch via the Principal's own client, then apply existing allow-list filter."""
    if not gate.allowed or gate.pg_client is None or gate.staff is None:
        return []
    use_kind = str(kind or gate.kind or "")
    if use_kind not in {"templates", "groups"}:
        return []
    if use_kind == "templates":
        primary = getattr(gate.pg_client, "get_user_templates", None)
        fallback = getattr(gate.pg_client, "get_user_templates_simple", None)
    else:
        primary = getattr(gate.pg_client, "get_groups", None)
        fallback = getattr(gate.pg_client, "get_groups_simple", None)
    if primary is None and fallback is None:
        return []
    raw = await primary() if primary is not None else await fallback()
    items = _unwrap_list(raw, use_kind)
    # Match web panel: empty full list → try simple endpoint.
    if not items and primary is not None and fallback is not None and fallback is not primary:
        raw = await fallback()
        items = _unwrap_list(raw, use_kind)
    # Same trust rule as web ``filter_*_for_staff`` default (own-client keep;
    # no credentials → fail closed). Never hardcode False — that emptied
    # reseller bot lists when PasarGuard role has no explicit allow-list.
    if use_kind == "templates":
        return filter_templates_for_staff(items, gate.staff)
    return filter_groups_for_staff(items, gate.staff)
