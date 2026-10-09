"""Phase 2B / 3C — OrgPrincipal Web identity / login (Level-1 and Level-2).

Web login is the Principal's PasarGuard username (same contract as pg_staff).
Never trusts client principal_id / depth / parent_id / role names for identity.
Does not store or expose PG passwords in session cookies.

Level-2 uses the same identity table, cookie shape, and resolve path as Level-1.
Parent L1 must be active on every login and protected request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrgPrincipal, OrgPrincipalWebIdentity, PgStaffAccess, ResellerProfile
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_TWO,
    STATUS_ACTIVE,
    attach_org_principal_fields,
    get_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.web_auth import hash_password, verify_password_hash

log = logging.getLogger(__name__)

# Session role string for Level-1 / Level-2 Principal web logins (not Owner).
ROLE_PRINCIPAL = "principal"


class PrincipalWebIdentityError(Exception):
    def __init__(self, message: str, *, code: str = "denied"):
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PrincipalWebAuthResult:
    identity: OrgPrincipalWebIdentity
    principal: OrgPrincipal
    web_username: str
    pg_username: str | None


def _norm_username(raw: str | None) -> str:
    return (raw or "").strip().lower()


def password_version(password_hash: str | None) -> str:
    """Cookie invalidation token — hash prefix only, never the password."""
    return (password_hash or "")[:24]


async def get_web_identity(
    session: AsyncSession, identity_id: int
) -> OrgPrincipalWebIdentity | None:
    if identity_id <= 0:
        return None
    return await session.get(OrgPrincipalWebIdentity, int(identity_id))


async def get_web_identity_by_username(
    session: AsyncSession, web_username: str
) -> OrgPrincipalWebIdentity | None:
    uname = _norm_username(web_username)
    if not uname:
        return None
    result = await session.execute(
        select(OrgPrincipalWebIdentity).where(
            OrgPrincipalWebIdentity.web_username == uname
        )
    )
    return result.scalar_one_or_none()


async def get_web_identity_for_principal(
    session: AsyncSession, principal_id: int
) -> OrgPrincipalWebIdentity | None:
    result = await session.execute(
        select(OrgPrincipalWebIdentity).where(
            OrgPrincipalWebIdentity.principal_id == int(principal_id)
        )
    )
    return result.scalar_one_or_none()


def _web_username_for_principal(principal: OrgPrincipal, web_username: str) -> str:
    """Web login must equal this Principal's PG username. No client rename."""
    pg = _norm_username(principal.pg_username)
    if not pg:
        raise PrincipalWebIdentityError(
            "نام کاربری پاسارگارد این نماینده موجود نیست",
            code="pg_username_missing",
        )
    posted = _norm_username(web_username)
    if posted != pg:
        raise PrincipalWebIdentityError(
            "نام کاربری پنل باید همان نام کاربری پاسارگارد باشد",
            code="web_pg_mismatch",
        )
    return pg


async def _assert_web_username_available(
    session: AsyncSession, principal: OrgPrincipal, uname: str
) -> None:
    """Deny usernames taken by another shop reseller or pg_staff row."""
    exclude_rid = 0
    try:
        exclude_rid = int(principal.reseller_profile_id or 0)
    except (TypeError, ValueError):
        exclude_rid = 0
    q_res = select(ResellerProfile.id).where(ResellerProfile.web_username == uname)
    if exclude_rid > 0:
        q_res = q_res.where(ResellerProfile.id != exclude_rid)
    if (await session.execute(q_res)).scalar_one_or_none() is not None:
        raise PrincipalWebIdentityError(
            "نام کاربری وب تکراری است",
            code="username_taken",
        )
    exclude_sid = 0
    try:
        exclude_sid = int(principal.pg_staff_id or 0)
    except (TypeError, ValueError):
        exclude_sid = 0
    q_staff = select(PgStaffAccess.id).where(PgStaffAccess.web_username == uname)
    if exclude_sid > 0:
        q_staff = q_staff.where(PgStaffAccess.id != exclude_sid)
    if (await session.execute(q_staff)).scalar_one_or_none() is not None:
        raise PrincipalWebIdentityError(
            "نام کاربری وب تکراری است",
            code="username_taken",
        )


def _assert_not_owner(principal: OrgPrincipal) -> None:
    if is_owner_principal(principal):
        raise PrincipalWebIdentityError(
            "هویت وب Principal نمی‌تواند مالک باشد",
            code="cannot_be_owner",
        )


def _assert_level1_principal(principal: OrgPrincipal | None) -> OrgPrincipal:
    """Level-1 only — used by attach_level1_web_identity."""
    if principal is None:
        raise PrincipalWebIdentityError(
            "Principal یافت نشد",
            code="principal_missing",
        )
    if str(principal.status) != STATUS_ACTIVE:
        raise PrincipalWebIdentityError(
            "Principal غیرفعال است",
            code="principal_disabled",
        )
    _assert_not_owner(principal)
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError) as exc:
        raise PrincipalWebIdentityError(
            "عمق Principal نامعتبر است",
            code="depth_invalid",
        ) from exc
    if depth != DEPTH_ONE:
        raise PrincipalWebIdentityError(
            "فقط Principal سطح ۱ می‌تواند از این مسیر هویت وب بگیرد",
            code="not_level1",
        )
    if principal.parent_id is None:
        raise PrincipalWebIdentityError(
            "Principal سطح ۱ باید والد داشته باشد",
            code="parent_missing",
        )
    return principal


def _assert_level2_principal(principal: OrgPrincipal | None) -> OrgPrincipal:
    """Level-2 only — used by attach_level2_web_identity (parent checked async)."""
    if principal is None:
        raise PrincipalWebIdentityError(
            "Principal یافت نشد",
            code="principal_missing",
        )
    if str(principal.status) != STATUS_ACTIVE:
        raise PrincipalWebIdentityError(
            "Principal غیرفعال است",
            code="principal_disabled",
        )
    _assert_not_owner(principal)
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError) as exc:
        raise PrincipalWebIdentityError(
            "عمق Principal نامعتبر است",
            code="depth_invalid",
        ) from exc
    if depth != DEPTH_TWO:
        raise PrincipalWebIdentityError(
            "فقط Principal سطح ۲ می‌تواند از این مسیر هویت وب بگیرد",
            code="not_level2",
        )
    if principal.parent_id is None:
        raise PrincipalWebIdentityError(
            "Principal سطح ۲ باید والد سطح ۱ داشته باشد",
            code="parent_missing",
        )
    return principal


async def _assert_active_level1_parent(
    session: AsyncSession, principal: OrgPrincipal
) -> OrgPrincipal:
    """Fail closed when L2's parent is missing, inactive, or not Level-1."""
    try:
        parent_id = int(principal.parent_id) if principal.parent_id is not None else 0
    except (TypeError, ValueError):
        parent_id = 0
    if parent_id <= 0:
        raise PrincipalWebIdentityError(
            "Principal سطح ۲ باید والد سطح ۱ داشته باشد",
            code="parent_missing",
        )
    parent = await get_principal(session, parent_id)
    if parent is None:
        raise PrincipalWebIdentityError(
            "والد Principal یافت نشد",
            code="parent_missing",
        )
    if str(parent.status) != STATUS_ACTIVE:
        raise PrincipalWebIdentityError(
            "والد Principal غیرفعال است",
            code="parent_disabled",
        )
    try:
        pdepth = int(parent.depth)
    except (TypeError, ValueError):
        pdepth = -1
    if pdepth != DEPTH_ONE:
        raise PrincipalWebIdentityError(
            "والد Principal سطح ۲ باید سطح ۱ باشد",
            code="parent_invalid",
        )
    return parent


async def _assert_login_principal(
    session: AsyncSession, principal: OrgPrincipal | None
) -> OrgPrincipal:
    """Server-side hierarchy gate for login + protected request resolve (L1 or L2)."""
    if principal is None:
        raise PrincipalWebIdentityError(
            "Principal یافت نشد",
            code="principal_missing",
        )
    if str(principal.status) != STATUS_ACTIVE:
        raise PrincipalWebIdentityError(
            "Principal غیرفعال است",
            code="principal_disabled",
        )
    _assert_not_owner(principal)
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError) as exc:
        raise PrincipalWebIdentityError(
            "عمق Principal نامعتبر است",
            code="depth_invalid",
        ) from exc
    if depth == DEPTH_ONE:
        if principal.parent_id is None:
            raise PrincipalWebIdentityError(
                "Principal سطح ۱ باید والد داشته باشد",
                code="parent_missing",
            )
        return principal
    if depth == DEPTH_TWO:
        _assert_level2_principal(principal)
        await _assert_active_level1_parent(session, principal)
        return principal
    raise PrincipalWebIdentityError(
        "این عمق Principal هویت وب ندارد",
        code="depth_invalid",
    )


def _assert_pg_identity_ready(principal: OrgPrincipal) -> None:
    """Level-2 must have its own stored PG identity — no parent/Owner fallback.

    Does not decrypt (decrypt stays in ``get_pg_for_principal``). Empty username
    or missing ciphertext → DENY. Level-1 may still log in without ciphertext
    (Phase 2B); PG ops fail closed later.
    """
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        depth = -1
    if depth != DEPTH_TWO:
        return
    uname = (principal.pg_username or "").strip()
    if not uname:
        raise PrincipalWebIdentityError(
            "هویت پاسارگارد Principal سطح ۲ موجود نیست",
            code="pg_identity_missing",
        )
    enc = (principal.pg_password_enc or "").strip() or (
        getattr(principal, "pg_api_key_enc", None) or ""
    ).strip()
    if not enc:
        raise PrincipalWebIdentityError(
            "اعتبارنامه پاسارگارد Principal سطح ۲ موجود نیست",
            code="pg_credential_missing",
        )


async def _create_web_identity_row(
    session: AsyncSession,
    *,
    principal: OrgPrincipal,
    web_username: str,
    password: str,
    is_active: bool,
) -> OrgPrincipalWebIdentity:
    uname = _norm_username(web_username)
    if not uname or len(uname) < 3:
        raise PrincipalWebIdentityError(
            "نام کاربری وب نامعتبر است",
            code="username_invalid",
        )
    pwd = (password or "").strip()
    if not pwd:
        raise PrincipalWebIdentityError(
            "رمز عبور الزامی است",
            code="password_required",
        )

    # Username must not collide with Owner web admin username.
    try:
        from app.services.web_auth import load_web_admin

        owner_u = _norm_username(load_web_admin().get("username"))
        if owner_u and uname == owner_u:
            raise PrincipalWebIdentityError(
                "نام کاربری با ادمین اصلی یکی است",
                code="owner_username_forbidden",
            )
    except PrincipalWebIdentityError:
        raise
    except Exception:
        pass

    existing_for_principal = await get_web_identity_for_principal(
        session, int(principal.id)
    )
    if existing_for_principal is not None:
        raise PrincipalWebIdentityError(
            "این Principal از قبل هویت وب دارد",
            code="already_mapped",
        )

    taken = await get_web_identity_by_username(session, uname)
    if taken is not None:
        raise PrincipalWebIdentityError(
            "نام کاربری وب تکراری است",
            code="username_taken",
        )
    await _assert_web_username_available(session, principal, uname)

    row = OrgPrincipalWebIdentity(
        principal_id=int(principal.id),
        web_username=uname,
        web_password_hash=hash_password(pwd),
        is_active=bool(is_active),
    )
    session.add(row)
    await session.flush()
    return row


async def attach_level1_web_identity(
    session: AsyncSession,
    *,
    principal_id: int,
    web_username: str,
    password: str,
    is_active: bool = True,
) -> OrgPrincipalWebIdentity:
    """Create Web credentials for an existing Level-1 Principal.

    Does not create Principals, bots, or depth-2 children. Password stored hashed only.
    Web username must equal ``principal.pg_username``.
    """
    principal = _assert_level1_principal(await get_principal(session, int(principal_id)))
    return await _create_web_identity_row(
        session,
        principal=principal,
        web_username=_web_username_for_principal(principal, web_username),
        password=password,
        is_active=is_active,
    )


async def attach_level2_web_identity(
    session: AsyncSession,
    *,
    principal_id: int,
    web_username: str,
    password: str,
    is_active: bool = True,
) -> OrgPrincipalWebIdentity:
    """Create Web credentials for an existing Level-2 Principal (same table as L1).

    Does not provision Principals, PG accounts, bots, or UI. Parent L1 must be active.
    Web username must equal ``principal.pg_username``.
    """
    principal = _assert_level2_principal(await get_principal(session, int(principal_id)))
    await _assert_active_level1_parent(session, principal)
    return await _create_web_identity_row(
        session,
        principal=principal,
        web_username=_web_username_for_principal(principal, web_username),
        password=password,
        is_active=is_active,
    )


async def authenticate_level1_web(
    session: AsyncSession,
    *,
    username: str,
    password: str,
) -> PrincipalWebAuthResult | None:
    """Verify Web credentials → Level-1 or Level-2 Principal.

    Same login path for both depths. Returns None on bad credentials.
    """
    uname = _norm_username(username)
    if not uname or not (password or ""):
        return None
    identity = await get_web_identity_by_username(session, uname)
    if identity is None:
        return None
    if not verify_password_hash(password, identity.web_password_hash):
        return None
    if not bool(identity.is_active):
        raise PrincipalWebIdentityError(
            "حساب وب غیرفعال است",
            code="identity_disabled",
        )
    principal = await _assert_login_principal(
        session, await get_principal(session, int(identity.principal_id))
    )
    _assert_pg_identity_ready(principal)
    pg_username = (principal.pg_username or "").strip() or None
    return PrincipalWebAuthResult(
        identity=identity,
        principal=principal,
        web_username=str(identity.web_username),
        pg_username=pg_username,
    )


# Same authenticator — L2 does not get a second login system.
authenticate_principal_web = authenticate_level1_web


def build_principal_session_payload(
    auth: PrincipalWebAuthResult,
    *,
    permissions: list[str] | None = None,
    pg_permissions: list[str] | None = None,
    pg_user_actions: dict | None = None,
    pg_access: dict | None = None,
    pg_writes: dict | None = None,
    pg_role_id: int | None = None,
) -> dict[str, Any]:
    """Cookie payload — identity lookup keys only; no PG password, no trusted scope."""
    payload: dict[str, Any] = {
        "role": ROLE_PRINCIPAL,
        "username": auth.web_username,
        "web_identity_id": int(auth.identity.id),
        "pv": password_version(auth.identity.web_password_hash),
        "permissions": list(permissions or []),
        "pg_permissions": list(pg_permissions or []),
    }
    # PG username for display/gate — not credentials. Server re-binds from Principal.
    if auth.pg_username:
        payload["pg_admin_username"] = auth.pg_username
    if pg_user_actions:
        payload["pg_user_actions"] = dict(pg_user_actions)
    if pg_access:
        payload["pg_access"] = dict(pg_access)
    if pg_writes:
        payload["pg_writes"] = dict(pg_writes)
    if pg_role_id is not None:
        payload["pg_role_id"] = int(pg_role_id)
    # Explicitly omit plaintext secrets and hierarchy trust fields.
    for banned in (
        "password",
        "pg_password",
        "pg_password_enc",
        "pg_api_key",
        "pg_api_key_enc",
        "web_password",
        "panel_password",
        "org_principal_id",
        "org_depth",
        "org_parent_id",
        "org_visible_principal_ids",
        "web_owner",
    ):
        payload.pop(banned, None)
    return payload


def session_contains_plaintext_secret(payload: Mapping[str, Any] | None) -> bool:
    """True if session carries password material (plaintext or ciphertext blob)."""
    if not payload:
        return False
    # Ciphertext must never ride in cookies either.
    if payload.get("pg_password_enc") or payload.get("pg_api_key_enc"):
        return True
    for key in (
        "password",
        "pg_password",
        "pg_api_key",
        "web_password",
        "panel_password",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            # bcrypt hashes are web password versions only when prefixed $2 — still forbid.
            return True
    return False


async def resolve_principal_web_session(
    session: AsyncSession,
    cookie_user: Mapping[str, Any],
) -> dict[str, Any]:
    """Server-side resolve for ``require_staff`` when role=principal.

    Ignores client-supplied principal_id / depth / parent / scope. DENY when
    identity or Principal is missing/disabled or hierarchy is wrong.
    """
    from app.services.pg_staff_access import PG_ACCESS_DENIED_MSG

    if (cookie_user.get("role") or "") != ROLE_PRINCIPAL:
        raise PrincipalWebIdentityError("نقش نشست نامعتبر است", code="bad_role")

    try:
        identity_id = int(cookie_user.get("web_identity_id") or 0)
    except (TypeError, ValueError):
        identity_id = 0
    if identity_id <= 0:
        raise PrincipalWebIdentityError(
            "نگاشت Principal موجود نیست",
            code="missing_mapping",
        )

    identity = await get_web_identity(session, identity_id)
    if identity is None:
        raise PrincipalWebIdentityError(
            "نگاشت Principal موجود نیست",
            code="missing_mapping",
        )
    if not bool(identity.is_active):
        raise PrincipalWebIdentityError(
            PG_ACCESS_DENIED_MSG,
            code="identity_disabled",
        )

    # Username + password-version binding (same pattern as reseller/pg_staff).
    cookie_user_name = _norm_username(str(cookie_user.get("username") or ""))
    if cookie_user_name != _norm_username(identity.web_username):
        raise PrincipalWebIdentityError(
            "هویت نشست نامعتبر است",
            code="username_mismatch",
        )
    expected_pv = password_version(identity.web_password_hash)
    if expected_pv and cookie_user.get("pv") != expected_pv:
        raise PrincipalWebIdentityError(
            "نشست منقضی شده است",
            code="pv_mismatch",
        )

    principal = await _assert_login_principal(
        session, await get_principal(session, int(identity.principal_id))
    )
    _assert_pg_identity_ready(principal)

    # Reject cookie attempts to spoof another Principal / Owner.
    raw_cookie_pid = cookie_user.get("org_principal_id")
    if raw_cookie_pid is not None:
        try:
            spoof = int(raw_cookie_pid)
        except (TypeError, ValueError):
            spoof = -1
        if spoof != int(principal.id):
            raise PrincipalWebIdentityError(
                "دستکاری شناسه Principal رد شد",
                code="principal_tamper",
            )

    for field, expected in (
        ("org_depth", int(principal.depth)),
        ("org_parent_id", int(principal.parent_id) if principal.parent_id is not None else None),
    ):
        if field not in cookie_user:
            continue
        raw = cookie_user.get(field)
        if field == "org_parent_id" and raw is None and expected is None:
            continue
        try:
            got = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            got = -1
        if got != expected:
            raise PrincipalWebIdentityError(
                "دستکاری سلسله‌مراتب رد شد",
                code="hierarchy_tamper",
            )

    visible = await visible_principal_ids(session, principal)
    out = attach_org_principal_fields(
        dict(cookie_user),
        principal,
        visible_principal_ids=visible,
    )
    out["role"] = ROLE_PRINCIPAL
    out["username"] = identity.web_username
    out["web_identity_id"] = int(identity.id)
    out["web_owner"] = False
    out["permissions"] = []
    # PG identity belongs to this Principal — never Owner env credentials.
    out["pg_admin_username"] = (principal.pg_username or "").strip() or None
    out.pop("password", None)
    out.pop("pg_password", None)
    out.pop("pg_password_enc", None)
    out.pop("pg_api_key", None)
    out.pop("pg_api_key_enc", None)

    # Refresh PG capability matrices from this Principal's PG admin (not Owner).
    from app.services.pg_access import enrich_staff_pg_from_role, resolve_acl_from_client
    from app.services.principal_pg_authz import apply_level1_pg_local_safety

    pg_uname = out.get("pg_admin_username")
    if not pg_uname:
        # Missing PG identity → fail closed (no Owner capability fallback).
        out["pg_permissions"] = []
        out["pg_actions"] = {}
        out["pg_user_actions"] = {}
        out["pg_writes"] = {}
        out["pg_credentials_ready"] = False
        out["pg_capabilities_ok"] = False
        out = apply_level1_pg_local_safety(out, None)
        out = attach_org_principal_fields(
            out, principal, visible_principal_ids=visible
        )
        out["web_owner"] = False
        return await _attach_linked_shop_package(session, out, principal)

    # Live PG role only. Cookie ``pg_role_id`` is untrusted: a stale or injected
    # id plus the shared ``_ROLE_CACHE`` would keep/escalate capabilities after
    # PG outage, role shrink, or admin deletion. Missing live lookup → deny.
    # L1/L2 must use this Principal's own client — never Owner get_pg().
    from app.services.pasarguard import get_pg_for_principal

    pg_client = None
    try:
        pg_client = await get_pg_for_principal(
            session, principal_id=int(principal.id)
        )
    except Exception:
        pg_client = None
    role = None
    if pg_client is not None:
        features, role, _admin = await resolve_acl_from_client(
            pg_client, username=pg_uname
        )
        if not features and role is None:
            out["pg_permissions"] = []
            out["pg_actions"] = {}
            out["pg_user_actions"] = {}
            out["pg_writes"] = {}
            out["pg_capabilities_ok"] = False
        else:
            out = enrich_staff_pg_from_role(out, features, role)
            out["pg_capabilities_ok"] = True
            if isinstance(role, dict) and role.get("id") is not None:
                try:
                    out["pg_role_id"] = int(role["id"])
                except (TypeError, ValueError):
                    pass
    else:
        out["pg_permissions"] = []
        out["pg_actions"] = {}
        out["pg_user_actions"] = {}
        out["pg_writes"] = {}
        out["pg_capabilities_ok"] = False

    out["pg_credentials_ready"] = bool(
        (principal.pg_password_enc or "").strip()
        or (getattr(principal, "pg_api_key_enc", None) or "").strip()
    )
    out = apply_level1_pg_local_safety(out, role if isinstance(role, dict) else None)

    # Ensure attached principal fields win over any cookie hierarchy leftovers.
    out = attach_org_principal_fields(
        out, principal, visible_principal_ids=visible
    )
    out["web_owner"] = False
    out["pg_is_owner"] = False
    out = await _attach_linked_shop_package(session, out, principal)
    return out


async def _attach_linked_shop_package(
    session: AsyncSession,
    staff: dict[str, Any],
    principal: OrgPrincipal,
) -> dict[str, Any]:
    """Attach the linked ResellerProfile shop tenant without a second login."""
    from app.db.models import ResellerProfile
    from app.services.authz import resolve_shop_permissions_from_profile

    rpid = getattr(principal, "reseller_profile_id", None)
    try:
        rpid_i = int(rpid) if rpid is not None else 0
    except (TypeError, ValueError):
        rpid_i = 0
    if rpid_i <= 0:
        return staff
    profile = await session.get(ResellerProfile, rpid_i)
    if profile is None or not bool(profile.is_active):
        return staff
    out = dict(staff)
    out["reseller_profile_id"] = int(profile.id)
    try:
        out["bot_user_id"] = int(profile.user_id)
    except (TypeError, ValueError):
        pass
    perms = resolve_shop_permissions_from_profile(profile)
    if perms:
        out["permissions"] = list(perms)
    return out


def owner_env_pg_username() -> str:
    try:
        from app.config import get_settings

        return (get_settings().pg_username or "").strip()
    except Exception:
        return ""


def staff_uses_owner_pg_credentials(staff: Mapping[str, Any] | None) -> bool:
    """True when staff pg_admin_username equals Owner env PG username."""
    if not staff:
        return False
    owner_pg = owner_env_pg_username()
    if not owner_pg:
        return False
    mine = str(staff.get("pg_admin_username") or "").strip()
    return bool(mine) and mine.lower() == owner_pg.lower()
