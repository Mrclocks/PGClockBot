"""PasarGuard admins page for any staff whose own PasarGuard role allows admins.

The owner's «ادمین‌های پاسارگارد» page works with the platform account and also
manages web-panel access. This page instead works only through the signed-in
staff member's own PasarGuard client, so PasarGuard itself decides which admins
they see and change. Each button is shown only when the staff member's role
allows that action.

Admins that the web panel itself manages (the platform account, shops,
sub-admins with web access, representatives) cannot be changed here: deleting or
disabling them must go through the owner's flows, which also clean up the shop.
The signed-in staff member cannot disable or delete their own account here either.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import OrgPrincipal, PgStaffAccess, ResellerProfile
from app.services.credential_policy import validate_credentials
from app.services.db_safe import rollback_quiet
from app.services.pasarguard import PasarGuardError
from app.services.pg_access import role_resource_actions

logger = logging.getLogger(__name__)

ROLE_ADMINS_FEATURE = "pg_role_admins"
ADMIN_ACTIONS: tuple[str, ...] = ("read", "read_simple", "create", "update", "delete", "reset_usage")
# The page shows one panel's admins; a panel with more than this is cut, not paged.
ADMIN_LIST_LIMIT = 300
CREATED_NOTE = "created from PGClockBot"

AdminChange = Literal["disable", "enable", "reset_usage", "delete"]
CHANGE_PERMISSION: dict[str, str] = {
    "disable": "update",
    "enable": "update",
    "reset_usage": "reset_usage",
    "delete": "delete",
}

DENIED_MSG = "نقش شما در پاسارگارد اجازهٔ این کار را نمی‌دهد."
SELF_MSG = "حساب خودتان را از این صفحه نمی‌توانید غیرفعال یا حذف کنید."
MANAGED_MSG = (
    "این ادمین به وب‌پنل متصل است (ادمین اصلی، فروشگاه یا ادمین فرعی). "
    "تغییر آن فقط از طریق مالک پنل ممکن است."
)
OWNER_ROLE_MSG = "نقش ادمین اصلی را نمی‌توان به ادمین جدید داد."
ROLE_REQUIRED_MSG = "یک نقش برای ادمین جدید انتخاب کنید."


class RoleAdminError(ValueError):
    """A request this page refuses; ``message`` is safe to show the staff member."""


@dataclass(slots=True, frozen=True)
class RoleAdminRow:
    username: str
    role_name: str
    is_disabled: bool
    used_traffic: int
    data_limit: int
    total_users: int
    is_self: bool
    is_managed: bool


@dataclass(slots=True, frozen=True)
class RoleAdminsPage:
    rows: tuple[RoleAdminRow, ...]
    roles: tuple[dict[str, Any], ...]
    truncated: bool


def admin_actions_for_staff(staff: Mapping[str, Any] | None) -> dict[str, bool]:
    """What the staff member's live PasarGuard role allows on admins."""
    role = (staff or {}).get("pg_role")
    if (staff or {}).get("pg_is_owner") is True and not isinstance(role, Mapping):
        role = {"is_owner": True}
    flags = role_resource_actions(role if isinstance(role, Mapping) else None, "admins", ADMIN_ACTIONS)
    flags["list"] = flags["read"] or flags["read_simple"]
    return flags


def _norm(username: Any) -> str:
    return str(username or "").strip().lower()


def staff_pg_username(staff: Mapping[str, Any] | None) -> str:
    return _norm((staff or {}).get("pg_admin_username") or "")


async def managed_admin_usernames(session: AsyncSession, usernames: Iterable[str]) -> frozenset[str]:
    """Which of ``usernames`` (lowercased) the web panel itself manages."""
    names = sorted({_norm(u) for u in usernames if _norm(u)})
    if not names:
        return frozenset()
    found: set[str] = set()
    for column in (ResellerProfile.pg_admin_username, PgStaffAccess.pg_username, OrgPrincipal.pg_username):
        rows = await session.scalars(select(func.lower(column)).where(func.lower(column).in_(names)))
        found.update(r for r in rows if r)
    platform = _norm(get_settings().pg_username)
    if platform in names:
        found.add(platform)
    return frozenset(found)


def _int_field(admin: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = admin.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0


def _is_disabled(admin: Mapping[str, Any]) -> bool:
    if admin.get("is_disabled") is True:
        return True
    return str(admin.get("status") or "").strip().lower() == "disabled"


def _role_name(admin: Mapping[str, Any]) -> str:
    role = admin.get("role")
    if isinstance(role, Mapping):
        return str(role.get("name") or "").strip()
    return ""


async def _fetch_admins(client: Any, actions: Mapping[str, bool]) -> list[dict]:
    if actions.get("read"):
        try:
            return list(await client.get_admins())
        except PasarGuardError as exc:
            if not actions.get("read_simple") or exc.status_code not in (403, 404, 405):
                raise
    return list(await client.get_admins_simple())


async def _assignable_roles(client: Any) -> list[dict[str, Any]]:
    """Non-owner roles this staff member's own PasarGuard account can read."""
    roles = await client.get_admin_roles()
    return [
        r
        for r in roles or []
        if isinstance(r, dict) and r.get("id") is not None and not r.get("is_owner")
    ]


async def load_role_admins_page(
    session: AsyncSession, client: Any, staff: Mapping[str, Any]
) -> RoleAdminsPage:
    actions = admin_actions_for_staff(staff)
    if not actions["list"]:
        raise RoleAdminError(DENIED_MSG)
    raw = [a for a in await _fetch_admins(client, actions) if isinstance(a, dict) and _norm(a.get("username"))]
    truncated = len(raw) > ADMIN_LIST_LIMIT
    raw = raw[:ADMIN_LIST_LIMIT]
    roles = tuple(await _assignable_roles(client)) if actions["create"] else ()
    managed = await managed_admin_usernames(session, (a.get("username") for a in raw))
    await rollback_quiet(session)
    me = staff_pg_username(staff)
    rows = tuple(
        RoleAdminRow(
            username=str(a.get("username")).strip(),
            role_name=_role_name(a),
            is_disabled=_is_disabled(a),
            used_traffic=_int_field(a, "used_traffic", "traffic_used"),
            data_limit=_int_field(a, "data_limit"),
            total_users=_int_field(a, "total_users", "users_count"),
            is_self=bool(me) and _norm(a.get("username")) == me,
            is_managed=_norm(a.get("username")) in managed,
        )
        for a in raw
    )
    return RoleAdminsPage(rows=rows, roles=roles, truncated=truncated)


async def create_role_admin(
    client: Any,
    staff: Mapping[str, Any],
    *,
    username: str,
    password: str,
    role_id: int | None,
) -> str:
    """Create a PasarGuard admin with a non-owner role; returns the stored username."""
    if not admin_actions_for_staff(staff)["create"]:
        raise RoleAdminError(DENIED_MSG)
    uname, problem = validate_credentials(username, password, lowercase_username=False)
    if problem:
        raise RoleAdminError(problem)
    if role_id is None or isinstance(role_id, bool) or role_id <= 0:
        raise RoleAdminError(ROLE_REQUIRED_MSG)
    roles = await _assignable_roles(client)
    if not any(int(r["id"]) == role_id for r in roles):
        raise RoleAdminError(OWNER_ROLE_MSG)
    await client.create_admin(
        {"username": uname, "password": password, "role_id": role_id, "note": CREATED_NOTE}
    )
    return uname


async def change_role_admin(
    session: AsyncSession,
    client: Any,
    staff: Mapping[str, Any],
    *,
    username: str,
    change: AdminChange,
) -> None:
    """Disable, enable, reset usage of, or delete one admin the panel does not manage."""
    permission = CHANGE_PERMISSION.get(change)
    if permission is None:
        raise RoleAdminError(DENIED_MSG)
    if not admin_actions_for_staff(staff)[permission]:
        raise RoleAdminError(DENIED_MSG)
    target = _norm(username)
    if not target:
        raise RoleAdminError(DENIED_MSG)
    if target == staff_pg_username(staff):
        raise RoleAdminError(SELF_MSG)
    is_managed = target in await managed_admin_usernames(session, [target])
    # End the read before the panel call; no transaction stays open across the network.
    await rollback_quiet(session)
    if is_managed:
        raise RoleAdminError(MANAGED_MSG)
    uname = str(username).strip()
    if change == "delete":
        await client.delete_admin(uname)
    elif change == "reset_usage":
        await client.reset_admin_usage(uname)
    else:
        # PasarGuard's admin update reads ``status``; other keys are ignored.
        status = "disabled" if change == "disable" else "active"
        await client.modify_admin(uname, {"status": status})
