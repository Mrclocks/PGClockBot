"""Phase 2A — Level-1 Principal provisioning foundation.

Owner-only backend API to create a depth-1 Principal under the active Owner.
Parent/depth are always server-determined. PasarGuard role *names* never affect
hierarchy. No UI, no depth-2 children, no Bot rollout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.redact import user_safe_error
from app.db.models import OrgPrincipal, OrgPrincipalProvision
from app.services.authz import (
    authz_from_staff,
    is_explicit_org_owner,
)
from app.services.org_principals import (
    DEPTH_ONE,
    STATUS_ACTIVE,
    OrgPrincipalError,
    create_principal,
    ensure_owner_principal,
    get_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff

log = logging.getLogger(__name__)

PROVISION_STATUS_COMPLETED = "completed"


class PrincipalProvisionError(Exception):
    """Denied or failed Level-1 Principal provision."""

    def __init__(self, message: str, *, code: str = "denied"):
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Level1ProvisionRequest:
    """Inputs for Level-1 provision. Hierarchy fields from clients are ignored."""

    pg_username: str
    pg_password: str
    idempotency_key: str
    pg_role_id: int | None = None
    note: str | None = None
    # Ignored — accepted so callers/tests prove they cannot override hierarchy:
    parent_id: Any = None
    depth: Any = None
    role_name: Any = None


@dataclass(frozen=True)
class Level1ProvisionResult:
    principal: OrgPrincipal
    pg_username: str
    pg_role_id: int | None
    idempotency_key: str
    created: bool
    visible_principal_ids: frozenset[int]


def owner_has_pg_admin_create_capability(staff: Mapping[str, Any] | None) -> bool:
    """PasarGuard capability for creating admins / Level-1 Principals.

    Uses explicit PG owner flag or actual ``admins.create`` — never page
    visibility (``pg_admins``) and never role *names*.

    ``pg_is_owner is False`` is Hybrid Owner (live probe): only ``admins.create``.
    Missing flag keeps sudo (same default as ``pg_quota.staff_needs_quota_check``).
    """
    if not staff:
        return False
    if staff.get("pg_is_owner") is False:
        from app.services.pg_access import staff_has_pg_admins_create

        return staff_has_pg_admins_create(staff)
    return True


def assert_can_provision_level1(staff: Mapping[str, Any] | None) -> None:
    """AUTHENTICATED ∧ ACTIVE OWNER PRINCIPAL ∧ PG CAPABILITY ∧ LOCAL SAFETY gate."""
    if not staff:
        raise PrincipalProvisionError("احراز هویت نشده", code="unauthenticated")
    if not is_explicit_owner_staff(staff):
        raise PrincipalProvisionError(
            "فقط مالک صریح سازمان می‌تواند Principal سطح ۱ بسازد",
            code="not_owner",
        )
    ctx = authz_from_staff(staff)
    if not is_explicit_org_owner(ctx):
        raise PrincipalProvisionError(
            "مالک سازمان فعال نیست",
            code="inactive_owner",
        )
    if not owner_has_pg_admin_create_capability(staff):
        raise PrincipalProvisionError(
            "قابلیت ساخت ادمین پاسارگارد برای این حساب فعال نیست",
            code="pg_capability_denied",
        )


def _normalize_idempotency_key(raw: str | None) -> str:
    key = (raw or "").strip()
    if not key:
        raise PrincipalProvisionError(
            "کلید یکتای عملیات (idempotency) الزامی است",
            code="idempotency_required",
        )
    if len(key) > 128:
        raise PrincipalProvisionError(
            "کلید یکتای عملیات بیش از حد طولانی است",
            code="idempotency_invalid",
        )
    return key


def _owner_env_pg_username() -> str:
    try:
        from app.config import get_settings

        return (get_settings().pg_username or "").strip()
    except Exception:
        return ""


async def _get_provision_by_key(
    session: AsyncSession, key: str
) -> OrgPrincipalProvision | None:
    result = await session.execute(
        select(OrgPrincipalProvision).where(OrgPrincipalProvision.idempotency_key == key)
    )
    return result.scalar_one_or_none()


async def _principal_by_pg_username(
    session: AsyncSession, username: str
) -> OrgPrincipal | None:
    result = await session.execute(
        select(OrgPrincipal).where(OrgPrincipal.pg_username == username)
    )
    return result.scalar_one_or_none()


async def _resolve_pg_role(
    *,
    pg_role_id: int | None,
    role_name: Any,
) -> tuple[int | None, dict | None]:
    """Resolve role by id only. Role *name* never grants hierarchy authority.

    Rejects owner-equivalent PG roles (local safety). Arbitrary non-owner role
    names / ids are allowed.
    """
    _ = role_name  # intentionally unused for authority
    if pg_role_id is None:
        return None, None
    try:
        rid = int(pg_role_id)
    except (TypeError, ValueError) as exc:
        raise PrincipalProvisionError("نقش پاسارگارد نامعتبر است", code="role_invalid") from exc
    if rid <= 0:
        raise PrincipalProvisionError("نقش پاسارگارد نامعتبر است", code="role_invalid")

    from app.services.pasarguard import get_pg

    try:
        roles = await get_pg().get_admin_roles()
    except Exception as exc:
        raise PrincipalProvisionError(
            f"دریافت نقش‌های پاسارگارد ناموفق: {user_safe_error(exc)}",
            code="pg_roles_unavailable",
        ) from exc

    chosen = next(
        (r for r in (roles or []) if isinstance(r, dict) and int(r.get("id") or 0) == rid),
        None,
    )
    if chosen is None:
        raise PrincipalProvisionError(
            "نقش پاسارگارد یافت نشد",
            code="role_not_found",
        )
    # Local safety: never mint owner-equivalent PG admins for Level-1 Principals.
    if chosen.get("is_owner"):
        raise PrincipalProvisionError(
            "نمی‌توان نقش مالک پاسارگارد را به Principal سطح ۱ داد",
            code="owner_role_forbidden",
        )
    return rid, chosen


async def _create_pg_admin(
    *,
    username: str,
    password: str,
    role_id: int | None,
    note: str | None,
) -> None:
    from app.services.pasarguard import get_pg

    payload: dict[str, Any] = {
        "username": username,
        "password": password,
        "note": (note or "PGClockBot Level-1 Principal").strip()[:255],
        "is_sudo": False,
    }
    if role_id is not None:
        payload["role_id"] = int(role_id)
        payload.pop("is_sudo", None)
    try:
        await get_pg().create_admin(payload)
    finally:
        # Never leave plaintext password on the payload dict (exceptions/logging).
        payload.pop("password", None)


async def _compensate_delete_pg_admin(username: str) -> None:
    from app.services.pasarguard import get_pg

    try:
        await get_pg().delete_admin(username)
    except Exception as compensate_exc:
        # Username + exception type only — never log.exception (traceback may
        # retain PG API response bodies that echo request passwords).
        log.error(
            "Phase 2A compensation: failed to delete PG admin after provision "
            "abort (username=%s, error=%s)",
            (username or "")[:64],
            type(compensate_exc).__name__,
        )


async def provision_level1_principal(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    request: Level1ProvisionRequest,
) -> Level1ProvisionResult:
    """Create a depth-1 Principal under Owner with its own PG identity.

    Server always sets ``parent_id=Owner`` and ``depth=1``. Client ``parent_id``,
    ``depth``, and role *names* cannot override hierarchy.
    """
    assert_can_provision_level1(staff)

    key = _normalize_idempotency_key(request.idempotency_key)

    # Idempotent replay — do not create a second Principal.
    existing_prov = await _get_provision_by_key(session, key)
    if existing_prov is not None:
        principal = await get_principal(session, int(existing_prov.principal_id))
        if principal is None or str(principal.status) != STATUS_ACTIVE:
            raise PrincipalProvisionError(
                "رکورد یکتای عملیات به Principal معتبر اشاره نمی‌کند",
                code="idempotency_orphan",
            )
        try:
            replay_depth = int(principal.depth)
        except (TypeError, ValueError):
            replay_depth = -1
        if replay_depth != DEPTH_ONE:
            # L2 (and any non-L1) keys live in the same table — never return
            # another depth as an L1 provision result.
            raise PrincipalProvisionError(
                "کلید یکتای عملیات به Principal سطح ۱ اشاره نمی‌کند",
                code="idempotency_mismatch",
            )
        visible = await visible_principal_ids(session, principal)
        return Level1ProvisionResult(
            principal=principal,
            pg_username=str(existing_prov.pg_username),
            pg_role_id=(
                int(existing_prov.pg_role_id) if existing_prov.pg_role_id is not None else None
            ),
            idempotency_key=key,
            created=False,
            visible_principal_ids=visible,
        )

    from app.services.credential_policy import validate_credentials

    uname, cerr = validate_credentials(
        request.pg_username, request.pg_password, lowercase_username=False
    )
    if cerr:
        raise PrincipalProvisionError(cerr, code="credentials_invalid")

    owner_pg = _owner_env_pg_username()
    if owner_pg and uname.strip().lower() == owner_pg.lower():
        raise PrincipalProvisionError(
            "نام کاربری پاسارگارد مالک قابل استفاده برای Principal جدید نیست",
            code="owner_pg_forbidden",
        )

    # Local safety: ignore client hierarchy overrides (tests D/E).
    _ = request.parent_id
    _ = request.depth

    try:
        owner = await ensure_owner_principal(session)
    except OrgPrincipalError:
        raise PrincipalProvisionError("مالک سازمان فعال نیست", code="inactive_owner")
    if not is_owner_principal(owner):
        raise PrincipalProvisionError("مالک سازمان فعال نیست", code="inactive_owner")

    # Staff Owner must match the singleton Owner principal.
    try:
        staff_owner_id = int(staff.get("org_principal_id") or 0)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        staff_owner_id = 0
    if staff_owner_id != int(owner.id):
        raise PrincipalProvisionError(
            "هویت مالک جلسه با مالک سازمان هم‌خوان نیست",
            code="owner_mismatch",
        )

    dup = await _principal_by_pg_username(session, uname)
    if dup is not None:
        raise PrincipalProvisionError(
            f"Principal با نام پاسارگارد «{uname}» از قبل وجود دارد",
            code="pg_username_taken",
        )

    role_id, _role = await _resolve_pg_role(
        pg_role_id=request.pg_role_id,
        role_name=request.role_name,
    )

    pg_created = False
    principal: OrgPrincipal | None = None
    try:
        await _create_pg_admin(
            username=uname,
            password=request.pg_password,
            role_id=role_id,
            note=request.note,
        )
        pg_created = True

        from app.services.secret_box import encrypt_secret

        pwd_enc = encrypt_secret(request.pg_password)
        if not pwd_enc:
            raise PrincipalProvisionError(
                "رمز‌گذاری رمز پاسارگارد ناموفق بود",
                code="encrypt_failed",
            )

        # parent/depth ALWAYS server-side — never from request.
        principal = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=DEPTH_ONE,
            pg_username=uname,
            pg_password_enc=pwd_enc,
            reseller_profile_id=None,
            pg_staff_id=None,
            bot_user_id=None,
            status=STATUS_ACTIVE,
        )
        session.add(
            OrgPrincipalProvision(
                idempotency_key=key,
                principal_id=int(principal.id),
                pg_username=uname,
                pg_role_id=role_id,
                created_by_principal_id=int(owner.id),
                status=PROVISION_STATUS_COMPLETED,
            )
        )
        await session.flush()
        from app.services.principal_web_identity import (
            PrincipalWebIdentityError,
            attach_level1_web_identity,
        )

        try:
            await attach_level1_web_identity(
                session,
                principal_id=int(principal.id),
                web_username=uname,
                password=request.pg_password,
            )
        except PrincipalWebIdentityError as exc:
            raise PrincipalProvisionError(exc.message, code=exc.code) from None
        from app.services.representative_unification import (
            attach_shop_package_to_principal,
        )

        try:
            await attach_shop_package_to_principal(
                session, principal, pg_username=uname
            )
        except Exception as exc:
            from app.services.principal_child_provisioning import ChildProvisionError

            if isinstance(exc, ChildProvisionError):
                raise PrincipalProvisionError(exc.message, code=exc.code) from None
            raise
    except PrincipalProvisionError:
        if pg_created:
            await _compensate_delete_pg_admin(uname)
        if principal is not None and getattr(principal, "id", None):
            await session.delete(principal)
            try:
                await session.flush()
            except Exception:
                await session.rollback()
        raise
    except Exception as exc:
        if pg_created:
            await _compensate_delete_pg_admin(uname)
        # Do not leave a half-created privileged Principal in the session/DB.
        try:
            await session.rollback()
        except Exception as rollback_exc:
            log.error(
                "Phase 2A session rollback after provision failure (%s)",
                type(rollback_exc).__name__,
            )
        # Never embed exception text or chain cause — PG/create errors may echo
        # passwords. Type name only; break __cause__ so tracebacks stay clean.
        log.error(
            "Phase 2A/2C Level-1 provision failed (%s)",
            type(exc).__name__,
        )
        raise PrincipalProvisionError(
            "ساخت Principal ناموفق بود",
            code="provision_failed",
        ) from None

    assert principal is not None
    if is_owner_principal(principal):
        # Defensive — create_principal(depth=1) must never yield Owner.
        await _compensate_delete_pg_admin(uname)
        await session.delete(principal)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
        raise PrincipalProvisionError(
            "Principal جدید نمی‌تواند مالک باشد",
            code="became_owner",
        )

    visible = await visible_principal_ids(session, principal)
    # New Principal must not receive Owner scope.
    if int(owner.id) in visible:
        await _compensate_delete_pg_admin(uname)
        await session.delete(principal)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
        raise PrincipalProvisionError(
            "محدوده Principal جدید نامعتبر است",
            code="scope_leak",
        )

    return Level1ProvisionResult(
        principal=principal,
        pg_username=uname,
        pg_role_id=role_id,
        idempotency_key=key,
        created=True,
        visible_principal_ids=visible,
    )


def principal_uses_owner_pg_credentials(principal: OrgPrincipal | None) -> bool:
    """True when a Principal incorrectly points at Owner env PG username."""
    if principal is None:
        return False
    owner_pg = _owner_env_pg_username()
    if not owner_pg:
        return False
    mine = (principal.pg_username or "").strip()
    if not mine:
        return False
    return mine.lower() == owner_pg.lower()
