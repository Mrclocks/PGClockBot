"""Phase 3B — Level-1 → Level-2 child Principal provisioning foundation.

Eligible active Level-1 Principals create depth-2 children with independent
PasarGuard identities. Parent/depth are always server-determined from the
authenticated Principal. Web login uses the child's PG username. No Bot or
Owner→L2 by default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrgPrincipal, OrgPrincipalProvision
from app.services.authz import authz_from_staff, has_active_org_principal
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_TWO,
    STATUS_ACTIVE,
    assert_depth2_parent,
    create_principal,
    get_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.pg_admin_roles import AdminRoleError, resolve_new_admin_role_id
from app.services.platform_identity import is_explicit_owner_staff

log = logging.getLogger(__name__)

PROVISION_STATUS_COMPLETED = "completed"
# Namespace prefix so raw client keys never collide across creators (test N).
_IDEM_PREFIX = "l2"


class ChildProvisionError(Exception):
    """Denied or failed Level-2 child provision."""

    def __init__(self, message: str, *, code: str = "denied"):
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Level2ProvisionRequest:
    """Inputs for Level-2 child provision. Hierarchy fields from clients are ignored."""

    pg_username: str
    pg_password: str
    idempotency_key: str
    pg_role_id: int | None = None
    note: str | None = None
    # Accepted only so callers/tests prove they cannot override hierarchy:
    parent_id: Any = None
    depth: Any = None
    kind: Any = None
    role_name: Any = None


@dataclass(frozen=True)
class Level2ProvisionResult:
    principal: OrgPrincipal
    parent_id: int
    pg_username: str
    pg_role_id: int | None
    idempotency_key: str
    created: bool
    visible_principal_ids: frozenset[int]
    parent_visible_principal_ids: frozenset[int]


def _action_allowed(val: Any) -> bool:
    if val is True or val == 1 or val == "1":
        return True
    if isinstance(val, str) and val.strip().lower() in {"true", "yes", "allow", "allowed"}:
        return True
    return False


def has_pg_admin_create_capability(staff: Mapping[str, Any] | None) -> bool:
    """PasarGuard capability to create admin identities (capability only).

    Uses action matrix / raw role permissions / explicit flag — never role *names*
    and never ``pg_admins`` page visibility. Owner explicit ``pg_is_owner`` is
    handled by the Owner provision gate, not this L1 helper.
    """
    if not staff:
        return False
    if bool(staff.get("pg_can_create_admin")):
        return True
    from app.services.pg_access import staff_has_pg_admins_create

    return staff_has_pg_admins_create(staff)


def _client_depth_value(raw: Any) -> int | None:
    if raw is None or raw is False or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def assert_can_provision_level2_child(staff: Mapping[str, Any] | None) -> None:
    """AUTH ∧ ACTIVE L1 ∧ PG admin-create ∧ LOCAL SAFETY (no Owner→L2 default)."""
    if not staff:
        raise ChildProvisionError("احراز هویت نشده", code="unauthenticated")

    # Default product policy: Owner creates L1 only — not L2 children.
    if is_explicit_owner_staff(staff):
        raise ChildProvisionError(
            "مالک سازمان مستقیماً Principal سطح ۲ نمی‌سازد",
            code="owner_creates_l1_only",
        )

    ctx = authz_from_staff(staff)
    if not has_active_org_principal(ctx):
        raise ChildProvisionError(
            "Principal فعال نیست",
            code="inactive_or_missing_principal",
        )
    if ctx.depth != DEPTH_ONE:
        raise ChildProvisionError(
            "فقط Principal سطح ۱ می‌تواند فرزند سطح ۲ بسازد",
            code="not_level1",
        )
    if ctx.parent_id is None:
        raise ChildProvisionError(
            "والد سطح ۱ نامعتبر است",
            code="parent_missing",
        )
    if not has_pg_admin_create_capability(staff):
        raise ChildProvisionError(
            "قابلیت ساخت ادمین پاسارگارد برای این حساب فعال نیست",
            code="pg_capability_denied",
        )


def _normalize_client_idempotency_key(raw: str | None) -> str:
    key = (raw or "").strip()
    if not key:
        raise ChildProvisionError(
            "کلید یکتای عملیات (idempotency) الزامی است",
            code="idempotency_required",
        )
    # Leave room for ``l2:{parent_id}:`` prefix within DB 128-char column.
    if len(key) > 96:
        raise ChildProvisionError(
            "کلید یکتای عملیات بیش از حد طولانی است",
            code="idempotency_invalid",
        )
    return key


def scoped_idempotency_key(parent_id: int, client_key: str) -> str:
    """Bind idempotency to creator so siblings cannot replay/steal results."""
    return f"{_IDEM_PREFIX}:{int(parent_id)}:{client_key}"


def _owner_env_pg_username() -> str:
    try:
        from app.config import get_settings

        return (get_settings().pg_username or "").strip()
    except Exception:
        return ""


async def _get_provision_by_scoped_key(
    session: AsyncSession, scoped_key: str
) -> OrgPrincipalProvision | None:
    result = await session.execute(
        select(OrgPrincipalProvision).where(
            OrgPrincipalProvision.idempotency_key == scoped_key
        )
    )
    return result.scalar_one_or_none()


async def _principal_by_pg_username(
    session: AsyncSession, username: str
) -> OrgPrincipal | None:
    result = await session.execute(
        select(OrgPrincipal).where(OrgPrincipal.pg_username == username)
    )
    return result.scalar_one_or_none()


async def _load_active_level1_parent(
    session: AsyncSession, staff: Mapping[str, Any]
) -> OrgPrincipal:
    try:
        pid = int(staff.get("org_principal_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        raise ChildProvisionError(
            "Principal فعال نیست",
            code="inactive_or_missing_principal",
        )
    parent = await get_principal(session, pid)
    if parent is None or str(parent.status) != STATUS_ACTIVE:
        raise ChildProvisionError(
            "Principal والد غیرفعال است",
            code="parent_disabled",
        )
    if is_owner_principal(parent) or int(parent.depth) != DEPTH_ONE:
        raise ChildProvisionError(
            "فقط Principal سطح ۱ می‌تواند فرزند سطح ۲ بسازد",
            code="not_level1",
        )
    if parent.parent_id is None:
        raise ChildProvisionError(
            "والد سطح ۱ نامعتبر است",
            code="parent_missing",
        )
    # Server-side depth-2 parent rules (active L1 only).
    assert_depth2_parent(parent)
    return parent


async def _resolve_pg_role_via_parent(
    parent_pg,
    *,
    pg_role_id: int | None,
    role_name: Any,
) -> tuple[int | None, dict | None]:
    """Resolve role via parent's PG client. Role *name* never grants hierarchy."""
    _ = role_name
    if pg_role_id is None:
        try:
            return await resolve_new_admin_role_id(parent_pg, None), None
        except AdminRoleError as exc:
            raise ChildProvisionError(str(exc), code="role_required") from None
    try:
        rid = int(pg_role_id)
    except (TypeError, ValueError) as exc:
        raise ChildProvisionError("نقش پاسارگارد نامعتبر است", code="role_invalid") from exc
    if rid <= 0:
        raise ChildProvisionError("نقش پاسارگارد نامعتبر است", code="role_invalid")

    try:
        roles = await parent_pg.get_admin_roles()
    except Exception:
        raise ChildProvisionError(
            "دریافت نقش‌های پاسارگارد ناموفق بود",
            code="pg_roles_unavailable",
        ) from None

    chosen = next(
        (r for r in (roles or []) if isinstance(r, dict) and int(r.get("id") or 0) == rid),
        None,
    )
    if chosen is None:
        raise ChildProvisionError(
            "نقش پاسارگارد یافت نشد",
            code="role_not_found",
        )
    if chosen.get("is_owner"):
        raise ChildProvisionError(
            "نمی‌توان نقش مالک پاسارگارد را به فرزند سطح ۲ داد",
            code="owner_role_forbidden",
        )
    return rid, chosen


async def _create_pg_admin_via_parent(
    parent_pg,
    *,
    username: str,
    password: str,
    role_id: int | None,
    note: str | None,
) -> None:
    payload: dict[str, Any] = {
        "username": username,
        "password": password,
        "note": (note or "PGClockBot Level-2 Child").strip()[:255],
        "is_sudo": False,
    }
    if role_id is not None:
        payload["role_id"] = int(role_id)
        payload.pop("is_sudo", None)
    try:
        await parent_pg.create_admin(payload)
    finally:
        payload.pop("password", None)


async def _compensate_delete_pg_admin(parent_pg, username: str) -> None:
    try:
        await parent_pg.delete_admin(username)
    except Exception as compensate_exc:
        log.error(
            "Phase 3B compensation: failed to delete PG admin after provision "
            "abort (username=%s, error=%s)",
            (username or "")[:64],
            type(compensate_exc).__name__,
        )


async def provision_level2_child(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    request: Level2ProvisionRequest,
) -> Level2ProvisionResult:
    """Create a depth-2 child under the authenticated Level-1 Principal.

    Server always sets ``parent_id=authenticated L1`` and ``depth=2``.
    Client ``parent_id`` / ``depth`` / ``kind`` / role *names* cannot override hierarchy.
    """
    assert_can_provision_level2_child(staff)
    assert staff is not None

    # Dangerous client depth overrides — escalate attempts fail closed.
    client_depth = _client_depth_value(request.depth)
    if client_depth is not None and client_depth > DEPTH_TWO:
        raise ChildProvisionError(
            "عمق بیشتر از ۲ مجاز نیست",
            code="depth_forbidden",
        )
    if client_depth == 0:
        raise ChildProvisionError(
            "نمی‌توان فرزند را به مالک تبدیل کرد",
            code="cannot_become_owner",
        )
    # depth=1 (or 2 / None): ignored — server forces depth=2.
    _ = request.parent_id
    _ = request.kind

    parent = await _load_active_level1_parent(session, staff)
    parent_id = int(parent.id)

    client_key = _normalize_client_idempotency_key(request.idempotency_key)
    scoped_key = scoped_idempotency_key(parent_id, client_key)

    existing_prov = await _get_provision_by_scoped_key(session, scoped_key)
    if existing_prov is not None:
        # Creator binding — another Principal's scoped key cannot hit this row.
        if int(existing_prov.created_by_principal_id) != parent_id:
            raise ChildProvisionError(
                "کلید یکتای عملیات متعلق به این Principal نیست",
                code="idempotency_forbidden",
            )
        child = await get_principal(session, int(existing_prov.principal_id))
        if child is None or str(child.status) != STATUS_ACTIVE:
            raise ChildProvisionError(
                "رکورد یکتای عملیات به Principal معتبر اشاره نمی‌کند",
                code="idempotency_orphan",
            )
        if int(child.depth) != DEPTH_TWO or int(child.parent_id or 0) != parent_id:
            raise ChildProvisionError(
                "رکورد یکتای عملیات با سلسله‌مراتب هم‌خوان نیست",
                code="idempotency_orphan",
            )
        child_vis = await visible_principal_ids(session, child)
        parent_vis = await visible_principal_ids(session, parent)
        return Level2ProvisionResult(
            principal=child,
            parent_id=parent_id,
            pg_username=str(existing_prov.pg_username),
            pg_role_id=(
                int(existing_prov.pg_role_id) if existing_prov.pg_role_id is not None else None
            ),
            idempotency_key=client_key,
            created=False,
            visible_principal_ids=child_vis,
            parent_visible_principal_ids=parent_vis,
        )

    from app.services.credential_policy import validate_credentials

    uname, cerr = validate_credentials(
        request.pg_username, request.pg_password, lowercase_username=False
    )
    if cerr:
        raise ChildProvisionError(cerr, code="credentials_invalid")

    owner_pg = _owner_env_pg_username()
    if owner_pg and uname.strip().lower() == owner_pg.lower():
        raise ChildProvisionError(
            "نام کاربری پاسارگارد مالک قابل استفاده برای فرزند نیست",
            code="owner_pg_forbidden",
        )
    parent_pg_uname = (parent.pg_username or "").strip()
    if parent_pg_uname and uname.strip().lower() == parent_pg_uname.lower():
        raise ChildProvisionError(
            "نام کاربری پاسارگارد والد قابل استفاده برای فرزند نیست",
            code="parent_pg_forbidden",
        )

    dup = await _principal_by_pg_username(session, uname)
    if dup is not None:
        raise ChildProvisionError(
            f"Principal با نام پاسارگارد «{uname}» از قبل وجود دارد",
            code="pg_username_taken",
        )

    from app.services.pasarguard import get_pg_for_principal

    try:
        parent_pg = await get_pg_for_principal(session, principal_id=parent_id)
    except Exception:
        raise ChildProvisionError(
            "کلاینت پاسارگارد والد در دسترس نیست",
            code="parent_pg_unavailable",
        ) from None

    role_id, _role = await _resolve_pg_role_via_parent(
        parent_pg,
        pg_role_id=request.pg_role_id,
        role_name=request.role_name,
    )

    pg_created = False
    child: OrgPrincipal | None = None
    try:
        await _create_pg_admin_via_parent(
            parent_pg,
            username=uname,
            password=request.pg_password,
            role_id=role_id,
            note=request.note,
        )
        pg_created = True

        from app.services.secret_box import encrypt_secret

        pwd_enc = encrypt_secret(request.pg_password)
        if not pwd_enc:
            raise ChildProvisionError(
                "رمز‌گذاری رمز پاسارگارد ناموفق بود",
                code="encrypt_failed",
            )

        # parent/depth ALWAYS server-side — never from request.
        child = await create_principal(
            session,
            parent_id=parent_id,
            depth=DEPTH_TWO,
            pg_username=uname,
            pg_password_enc=pwd_enc,
            reseller_profile_id=None,
            pg_staff_id=None,
            bot_user_id=None,
            status=STATUS_ACTIVE,
        )
        session.add(
            OrgPrincipalProvision(
                idempotency_key=scoped_key,
                principal_id=int(child.id),
                pg_username=uname,
                pg_role_id=role_id,
                created_by_principal_id=parent_id,
                status=PROVISION_STATUS_COMPLETED,
            )
        )
        await session.flush()
        from app.services.principal_web_identity import (
            PrincipalWebIdentityError,
            attach_level2_web_identity,
        )

        try:
            await attach_level2_web_identity(
                session,
                principal_id=int(child.id),
                web_username=uname,
                password=request.pg_password,
            )
        except PrincipalWebIdentityError as exc:
            raise ChildProvisionError(exc.message, code=exc.code) from None
        from app.services.representative_unification import (
            attach_shop_package_to_child_principal,
        )

        await attach_shop_package_to_child_principal(
            session, child, pg_username=uname
        )
    except ChildProvisionError:
        if pg_created:
            await _compensate_delete_pg_admin(parent_pg, uname)
        if child is not None and getattr(child, "id", None):
            await session.delete(child)
            try:
                await session.flush()
            except Exception:
                await session.rollback()
        raise
    except Exception as exc:
        if pg_created:
            await _compensate_delete_pg_admin(parent_pg, uname)
        try:
            await session.rollback()
        except Exception as rollback_exc:
            log.error(
                "Phase 3B session rollback after provision failure (%s)",
                type(rollback_exc).__name__,
            )
        log.error(
            "Phase 3B Level-2 child provision failed (%s)",
            type(exc).__name__,
        )
        raise ChildProvisionError(
            "ساخت Principal فرزند ناموفق بود",
            code="provision_failed",
        ) from None

    assert child is not None
    if is_owner_principal(child) or int(child.depth) != DEPTH_TWO:
        await _compensate_delete_pg_admin(parent_pg, uname)
        await session.delete(child)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
        raise ChildProvisionError(
            "Principal فرزند نمی‌تواند مالک یا عمق نامعتبر باشد",
            code="became_owner",
        )
    if int(child.parent_id or 0) != parent_id:
        await _compensate_delete_pg_admin(parent_pg, uname)
        await session.delete(child)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
        raise ChildProvisionError(
            "والد فرزند با Principal احراز هویت‌شده هم‌خوان نیست",
            code="parent_mismatch",
        )

    child_vis = await visible_principal_ids(session, child)
    parent_vis = await visible_principal_ids(session, parent)

    # Child must see only self; must not see parent or leak Owner.
    if child_vis != frozenset({int(child.id)}):
        await _compensate_delete_pg_admin(parent_pg, uname)
        await session.delete(child)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
        raise ChildProvisionError(
            "محدوده Principal فرزند نامعتبر است",
            code="scope_leak",
        )
    if int(child.id) not in parent_vis:
        await _compensate_delete_pg_admin(parent_pg, uname)
        await session.delete(child)
        try:
            await session.flush()
        except Exception:
            await session.rollback()
        raise ChildProvisionError(
            "فرزند در محدوده والد نیست",
            code="scope_leak",
        )

    return Level2ProvisionResult(
        principal=child,
        parent_id=parent_id,
        pg_username=uname,
        pg_role_id=role_id,
        idempotency_key=client_key,
        created=True,
        visible_principal_ids=child_vis,
        parent_visible_principal_ids=parent_vis,
    )


def child_uses_parent_or_owner_pg_credentials(
    child: OrgPrincipal | None,
    parent: OrgPrincipal | None,
) -> bool:
    """True when child incorrectly shares Parent or Owner env PG username."""
    if child is None:
        return False
    mine = (child.pg_username or "").strip()
    if not mine:
        return False
    owner_pg = _owner_env_pg_username()
    if owner_pg and mine.lower() == owner_pg.lower():
        return True
    if parent is not None:
        parent_pg = (parent.pg_username or "").strip()
        if parent_pg and mine.lower() == parent_pg.lower():
            return True
    return False
