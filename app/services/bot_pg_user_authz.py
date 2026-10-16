"""Bot PG-user authorization (shared with Web Authz).

Telegram identity → OrgPrincipal → AuthzContext → live PG capability
→ resource scope → local safety → action.

Callback/message data may carry a PG user id only. Hierarchy / PG identity
fields are ignored and treated as tamper → DENY.

Shop bots are isolated from this platform-admin family.
Owner and L1 keep existing gates. Bound L2 uses the same formula with
self-only scope and ``get_pg_for_principal(L2.id)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerProfile
from app.services.authz import AuthDecision
from app.services.bot_principal_identity import (
    BotPrincipalResolution,
    bot_pg_client_for_resolution,
    callback_carries_principal_tamper,
    resolve_bot_pg_family_identity,
)
from app.services.pg_user_scope import filter_pg_users_for_staff, is_full_pg_owner, pg_user_in_staff_scope, pg_user_owner_username
from app.services.principal_pg_authz import (
    apply_level1_pg_local_safety,
    authorize_pg_page,
    authorize_pg_user_action,
    is_level1_principal_staff,
    principal_pg_authz_ready,
)

BotPgUserAction = Literal[
    "list",
    "search",
    "read",
    "create",
    "update",
    "disable",
    "enable",
    "reset",
    "revoke",
    "delete",
]

_COLLECTION_ACTIONS = frozenset({"list", "search", "create"})
_MUTATE_ID_ACTIONS = frozenset(
    {"update", "disable", "enable", "reset", "revoke", "delete"}
)

# Existing PG user-action keys (role_user_actions / can_pg_user_action).
_PG_USER_ACTION = {
    "create": "create",
    "update": "update",
    "disable": "disable",
    "enable": "enable",
    "reset": "reset_usage",
    "revoke": "revoke_sub",
    "delete": "delete",
}

_DETAIL_RE = re.compile(r"^adm:pg:u:(\d+)$")
_LINK_RE = re.compile(r"^adm:pg:u:(\d+):link$")
_DISABLE_RE = re.compile(r"^adm:pg:dis:(\d+)$")
_ENABLE_RE = re.compile(r"^adm:pg:en:(\d+)$")
_RESET_RE = re.compile(r"^adm:pg:reset:(\d+)$")
_REVOKE_RE = re.compile(r"^adm:pg:rev:(\d+)$")
_EDIT_RE = re.compile(r"^adm:pg:u:(\d+):edit$")
_EDIT_NAME_RE = re.compile(r"^adm:pg:u:(\d+):ed:name$")
_EDIT_GB_RE = re.compile(r"^adm:pg:u:(\d+):ed:gb$")
_EDIT_DAYS_RE = re.compile(r"^adm:pg:u:(\d+):ed:days$")
_EDIT_GRPS_RE = re.compile(r"^adm:pg:u:(\d+):ed:grps$")
_DELASK_RE = re.compile(r"^adm:pg:u:(\d+):delask$")
_DEL_RE = re.compile(r"^adm:pg:u:(\d+):del$")

_ACTION_ID_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "read": (_DETAIL_RE, _LINK_RE),
    "disable": (_DISABLE_RE,),
    "enable": (_ENABLE_RE,),
    "reset": (_RESET_RE,),
    "revoke": (_REVOKE_RE,),
    "update": (
        _EDIT_RE,
        _EDIT_NAME_RE,
        _EDIT_GB_RE,
        _EDIT_DAYS_RE,
        _EDIT_GRPS_RE,
    ),
    "delete": (_DELASK_RE, _DEL_RE),
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
)

_USER_MESSAGES = {
    "unauthenticated": "ادمین نیستید",
    "shop_bot_isolated": "ادمین نیستید",
    "identity_tamper": "اجازه این عمل را ندارید",
    "inactive_or_missing_principal": "ادمین نیستید",
    "missing_principal": "ادمین نیستید",
    "pg_permission_denied": "اجازه این عمل را ندارید",
    "pg_capabilities_unavailable": "به کاربران پاسارگارد دسترسی ندارید",
    "resource_out_of_scope": "اجازه این عمل را ندارید",
    "resource_not_found": "اجازه این عمل را ندارید",
    "pg_outage": "به کاربران پاسارگارد دسترسی ندارید",
    "local_safety_denied": "اجازه این عمل را ندارید",
    "invalid_resource": "اجازه این عمل را ندارید",
    "missing_scope": "اجازه این عمل را ندارید",
}


@dataclass(frozen=True)
class BotPgUserGate:
    allowed: bool
    reason: str
    user_message: str
    resolution: BotPrincipalResolution | None = None
    staff: dict[str, Any] | None = None
    pg_user: dict[str, Any] | None = None
    pg_client: Any = None
    # Panel admins that belong to shops. A shop's customers are managed in the web panel only,
    # never from the platform Owner's personal bot.
    shop_pg_admins: frozenset[str] = frozenset()


def _deny(reason: str, *, resolution: BotPrincipalResolution | None = None) -> BotPgUserGate:
    return BotPgUserGate(
        allowed=False,
        reason=reason,
        user_message=_USER_MESSAGES.get(reason, "اجازه این عمل را ندارید"),
        resolution=resolution,
    )


def callback_carries_identity_tamper(callback_data: str | None) -> bool:
    """True when callback tries to inject principal / PG identity fields."""
    if callback_carries_principal_tamper(callback_data):
        return True
    if not callback_data:
        return False
    lowered = str(callback_data).lower()
    return any(needle in lowered for needle in _TAMPER_NEEDLES)


def parse_pg_user_callback_id(
    callback_data: str | None, *, kind: BotPgUserAction
) -> int | None:
    """Extract PG user id from a strict callback pattern. Extra fields → None."""
    if not callback_data:
        return None
    raw = str(callback_data).strip()
    patterns = _ACTION_ID_PATTERNS.get(str(kind)) or ()
    for match_re in patterns:
        match = match_re.match(raw)
        if match is None:
            continue
        try:
            uid = int(match.group(1))
        except (TypeError, ValueError):
            return None
        return uid if uid > 0 else None
    return None


def sanitize_pg_user_write_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop client-supplied owner/scope fields — Principal identity is server-side."""
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    for key in _CLIENT_OWNER_KEYS:
        out.pop(key, None)
    return out


async def shop_pg_admin_usernames(session: AsyncSession) -> frozenset[str]:
    """Panel admins of shops that run their own bot; admins without one are platform sub-admins."""
    rows = await session.scalars(
        select(func.lower(ResellerProfile.pg_admin_username)).where(
            ResellerProfile.pg_admin_username.is_not(None), ResellerProfile.pg_admin_username != "",
            ResellerProfile.bot_token.is_not(None), ResellerProfile.bot_token != ""
        )
    )
    return frozenset(name for name in rows if name)


def _is_shop_owned(user: dict[str, Any], shop_admins: frozenset[str]) -> bool:
    return pg_user_owner_username(user) in shop_admins


def _prepare_staff(resolution: BotPrincipalResolution) -> dict[str, Any] | None:
    staff = dict(resolution.staff)
    if is_level1_principal_staff(staff):
        staff = apply_level1_pg_local_safety(staff)
    if not principal_pg_authz_ready(staff):
        return None
    return staff


def _capability_decision(staff: dict[str, Any], action: BotPgUserAction) -> AuthDecision:
    if action in {"list", "search", "read"}:
        return authorize_pg_page(staff, "pg_users")
    pg_action = _PG_USER_ACTION.get(str(action))
    if not pg_action:
        return AuthDecision(False, "pg_permission_denied")
    return authorize_pg_user_action(staff, pg_action)


async def _resolve_identity(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None,
    is_reseller_bot: bool,
    reseller_profile_id: int | None,
    reseller_owner_id: int | None,
    callback_data: str | None,
) -> BotPgUserGate | BotPrincipalResolution:
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


async def authorize_bot_pg_user_op(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None,
    action: BotPgUserAction,
    callback_data: str | None = None,
    pg_user_id: int | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> BotPgUserGate:
    """Authorize a PG-user Bot operation (list/search/create or ID-based)."""
    ident = await _resolve_identity(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback_data=callback_data,
    )
    if isinstance(ident, BotPgUserGate):
        return ident
    resolution = ident

    staff = _prepare_staff(resolution)
    if staff is None:
        return _deny("pg_capabilities_unavailable", resolution=resolution)

    cap = _capability_decision(staff, action)
    if not cap.allowed:
        return BotPgUserGate(
            allowed=False,
            reason=cap.reason or "pg_permission_denied",
            user_message=_USER_MESSAGES.get(
                cap.reason, _USER_MESSAGES["pg_permission_denied"]
            ),
            resolution=resolution,
            staff=staff,
        )

    try:
        pg_client, _as_owner = await bot_pg_client_for_resolution(session, resolution)
    except Exception:
        return _deny("pg_outage", resolution=resolution)

    # Only the global Owner needs the filter; scoped principals already see just their own users.
    shop_admins = await shop_pg_admin_usernames(session) if is_full_pg_owner(staff) else frozenset()
    if action in _COLLECTION_ACTIONS:
        return BotPgUserGate(
            allowed=True,
            reason="ok",
            user_message="",
            resolution=resolution,
            staff=staff,
            pg_client=pg_client,
            shop_pg_admins=shop_admins,
        )

    uid = pg_user_id
    if uid is None:
        uid = parse_pg_user_callback_id(callback_data, kind=action)
    try:
        uid_i = int(uid) if uid is not None else 0
    except (TypeError, ValueError):
        uid_i = 0
    if uid_i <= 0:
        return _deny("invalid_resource", resolution=resolution)

    try:
        raw = await pg_client.get_user_by_id(uid_i)
    except Exception:
        return _deny("pg_outage", resolution=resolution)

    if not isinstance(raw, dict):
        return _deny("resource_not_found", resolution=resolution)
    if not pg_user_in_staff_scope(raw, staff) or _is_shop_owned(raw, shop_admins):
        return _deny("resource_out_of_scope", resolution=resolution)

    if action in _MUTATE_ID_ACTIONS:
        try:
            from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users

            await assert_can_mutate_owned_users(staff, session=session, client=pg_client)
        except PgQuotaError:
            return _deny("local_safety_denied", resolution=resolution)
        except Exception:
            return _deny("pg_outage", resolution=resolution)

    return BotPgUserGate(
        allowed=True,
        reason="ok",
        user_message="",
        resolution=resolution,
        staff=staff,
        pg_user=raw,
        pg_client=pg_client,
        shop_pg_admins=shop_admins,
    )


async def list_scoped_pg_users(
    gate: BotPgUserGate,
    *,
    page: int = 0,
    username: str | None = None,
    page_size: int = 10,
) -> tuple[list[dict], int | None]:
    """Fetch a page then clamp to Principal PG scope (unknown ownership dropped)."""
    if not gate.allowed or gate.pg_client is None or gate.staff is None:
        return [], None
    page = max(0, int(page))
    params: dict[str, Any] = {"offset": page * int(page_size), "limit": int(page_size)}
    q = (username or "").strip()
    if q:
        params["username"] = q
    data = await gate.pg_client.get_users(**params)
    if isinstance(data, list):
        users = [u for u in data if isinstance(u, dict)]
        total = None
    else:
        from app.services.pasarguard import as_list

        users = as_list(data, "users")
        total = None
        if isinstance(data, dict):
            for key in ("total", "count", "total_count"):
                if data.get(key) is not None:
                    try:
                        total = int(data[key])
                        break
                    except (TypeError, ValueError):
                        pass
    scoped = [u for u in filter_pg_users_for_staff(users, gate.staff) if not _is_shop_owned(u, gate.shop_pg_admins)]
    if total is not None and len(scoped) != len(users):
        total = None
    return scoped, total


async def lookup_scoped_pg_user_by_username(
    gate: BotPgUserGate, username: str
) -> dict[str, Any] | None:
    if not gate.allowed or gate.pg_client is None or gate.staff is None:
        return None
    q = (username or "").strip()
    if not q:
        return None
    try:
        raw = await gate.pg_client.get_user_by_username(q)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if not pg_user_in_staff_scope(raw, gate.staff) or _is_shop_owned(raw, gate.shop_pg_admins):
        return None
    return raw
