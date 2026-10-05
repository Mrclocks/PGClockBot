"""Org principal hierarchy foundation (Phase 1A).

Hierarchy is independent of PasarGuard role names and of session ``role=admin``.
Source of truth: ``org_principals`` rows. ResellerProfile / PgStaffAccess are
adapters only — never invent parents for legacy pg_staff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    OrgPrincipal,
    OrgPrincipalProvision,
    OrgPrincipalWebIdentity,
    PgStaffAccess,
    ResellerProfile,
    Ticket,
    UserService,
)

log = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

DEPTH_OWNER = 0
DEPTH_ONE = 1
DEPTH_TWO = 2
MAX_DEPTH = 2


class OrgPrincipalError(ValueError):
    """Invalid hierarchy / principal operation."""


@dataclass(frozen=True)
class LegacyUnassigned:
    """pg_staff (or other) with no reliable depth-1 parent — not in hierarchy."""

    pg_staff_id: int | None = None
    reason: str = "no_reliable_parent"


def validate_parent_depth(
    *,
    depth: int,
    parent: OrgPrincipal | None,
) -> None:
    """Enforce Owner/depth-1/depth-2 rules. Never allow depth-2 under Owner.

    Phase 3A — depth-2 MUST parent to an *active* Level-1 principal only.
    Rejects: Owner parent, depth-2 parent, depth>2, inactive parent, missing parent.
    """
    if depth < DEPTH_OWNER or depth > MAX_DEPTH:
        raise OrgPrincipalError(f"depth must be 0..{MAX_DEPTH}, got {depth}")
    if depth == DEPTH_OWNER:
        if parent is not None:
            raise OrgPrincipalError("Owner must have parent_id NULL")
        return
    if parent is None:
        raise OrgPrincipalError(f"depth {depth} requires a parent")
    if parent.status != STATUS_ACTIVE:
        raise OrgPrincipalError("parent principal is not active")
    if depth == DEPTH_ONE:
        if parent.depth != DEPTH_OWNER:
            raise OrgPrincipalError("depth-1 parent must be Owner (depth 0)")
        return
    # depth == 2 — Level-2 → Level-2 forbidden; Owner parent forbidden.
    if parent.depth != DEPTH_ONE:
        raise OrgPrincipalError("depth-2 parent must be depth-1 (not Owner)")


def assert_depth2_parent(parent: OrgPrincipal | None) -> OrgPrincipal:
    """Phase 3A — validate a candidate parent for a depth-2 child (no provisioning).

    Returns the parent when rules pass; raises ``OrgPrincipalError`` otherwise.
    Does not create Principals, credentials, Web identities, or bots.
    """
    validate_parent_depth(depth=DEPTH_TWO, parent=parent)
    assert parent is not None  # validated above
    return parent


def is_owner_principal(principal: OrgPrincipal | None) -> bool:
    """True only for an active depth-0 Owner row — not session role=admin."""
    if principal is None:
        return False
    return (
        int(principal.depth) == DEPTH_OWNER
        and principal.parent_id is None
        and str(principal.status) == STATUS_ACTIVE
    )


def role_string_implies_owner(role: str | None) -> bool:
    """Hierarchy authority never comes from session/bot role strings (incl. admin)."""
    _ = role
    return False


async def get_principal(session: AsyncSession, principal_id: int) -> OrgPrincipal | None:
    return await session.get(OrgPrincipal, int(principal_id))


async def get_active_owner(session: AsyncSession) -> OrgPrincipal | None:
    result = await session.execute(
        select(OrgPrincipal).where(
            OrgPrincipal.depth == DEPTH_OWNER,
            OrgPrincipal.parent_id.is_(None),
            OrgPrincipal.status == STATUS_ACTIVE,
        )
    )
    rows = list(result.scalars().all())
    if len(rows) > 1:
        raise OrgPrincipalError("multiple active Owner principals")
    return rows[0] if rows else None


async def ensure_owner_principal(session: AsyncSession) -> OrgPrincipal:
    """Return the single active Owner, or bootstrap one when none exists.

    Ordinary Bot/Web auth must not reactivate a disabled Owner and must not
    create a second depth-0 row. Bootstrap create runs only when zero
    depth-0 rows exist (first install / empty hierarchy).
    Does not read ``role=admin`` or PasarGuard role names.
    """
    existing = await get_active_owner(session)
    if existing is not None:
        return existing
    any_owner = await session.execute(
        select(OrgPrincipal).where(
            OrgPrincipal.depth == DEPTH_OWNER,
            OrgPrincipal.parent_id.is_(None),
        )
    )
    orphans = list(any_owner.scalars().all())
    if orphans:
        # Disabled/inactive Owner is a deny — never silent reactivation.
        if len(orphans) == 1 and str(orphans[0].status) != STATUS_ACTIVE:
            raise OrgPrincipalError("Owner principal is not active")
        raise OrgPrincipalError("Owner principal conflict; resolve manually")

    owner = OrgPrincipal(
        parent_id=None,
        depth=DEPTH_OWNER,
        status=STATUS_ACTIVE,
        pg_username=None,
        reseller_profile_id=None,
        pg_staff_id=None,
        bot_user_id=None,
    )
    validate_parent_depth(depth=DEPTH_OWNER, parent=None)
    try:
        async with session.begin_nested():
            session.add(owner)
            await session.flush()
        return owner
    except IntegrityError:
        session.expunge(owner)
        existing = await get_active_owner(session)
        if existing is not None:
            return existing
        raise OrgPrincipalError("Owner principal conflict; resolve manually")


async def create_principal(
    session: AsyncSession,
    *,
    parent_id: int | None,
    depth: int,
    pg_username: str | None = None,
    pg_password_enc: str | None = None,
    reseller_profile_id: int | None = None,
    pg_staff_id: int | None = None,
    bot_user_id: int | None = None,
    status: str = STATUS_ACTIVE,
) -> OrgPrincipal:
    """Insert a principal after parent/depth validation."""
    parent: OrgPrincipal | None = None
    if parent_id is not None:
        parent = await get_principal(session, parent_id)
        if parent is None:
            raise OrgPrincipalError("parent principal not found")
    validate_parent_depth(depth=depth, parent=parent)
    if depth == DEPTH_OWNER:
        any_owner = await session.execute(
            select(OrgPrincipal.id).where(
                OrgPrincipal.depth == DEPTH_OWNER,
                OrgPrincipal.parent_id.is_(None),
            ).limit(1)
        )
        if any_owner.scalar_one_or_none() is not None:
            raise OrgPrincipalError("Owner principal already exists")
    row = OrgPrincipal(
        parent_id=parent_id,
        depth=int(depth),
        status=str(status or STATUS_ACTIVE),
        pg_username=(pg_username or None),
        pg_password_enc=(pg_password_enc or None),
        reseller_profile_id=reseller_profile_id,
        pg_staff_id=pg_staff_id,
        bot_user_id=bot_user_id,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        if depth == DEPTH_OWNER:
            raise OrgPrincipalError("Owner principal already exists") from None
        raise
    return row


async def get_principal_by_reseller_profile(
    session: AsyncSession, reseller_profile_id: int
) -> OrgPrincipal | None:
    result = await session.execute(
        select(OrgPrincipal).where(
            OrgPrincipal.reseller_profile_id == int(reseller_profile_id)
        )
    )
    return result.scalar_one_or_none()


async def get_principal_by_pg_staff(
    session: AsyncSession, pg_staff_id: int
) -> OrgPrincipal | None:
    result = await session.execute(
        select(OrgPrincipal).where(OrgPrincipal.pg_staff_id == int(pg_staff_id))
    )
    return result.scalar_one_or_none()


async def map_reseller_profile_to_principal(
    session: AsyncSession,
    profile: ResellerProfile,
) -> OrgPrincipal | None:
    """Adapter: ResellerProfile → its linked Principal.

    Unmapped active shops become depth-1 under Owner (never L2). Existing
    mappings are returned at their stored depth (1 or 2). Client cannot
    choose parent/depth.
    """
    if profile is None or not getattr(profile, "id", None):
        return None
    pid = int(profile.id)
    existing = await get_principal_by_reseller_profile(session, pid)
    if existing is not None:
        return existing
    try:
        user_id = int(getattr(profile, "user_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    if not bool(getattr(profile, "is_active", True)):
        return None

    try:
        owner = await ensure_owner_principal(session)
    except OrgPrincipalError:
        return None
    # Shop L1 credentials stay on ResellerProfile and are used via
    # get_pg_for_reseller. Do not copy pg_admin_password_enc onto the adapter
    # Principal — Principal-web / get_pg_for_principal is not the credential
    # path for these rows.
    return await create_principal(
        session,
        parent_id=int(owner.id),
        depth=DEPTH_ONE,
        pg_username=(getattr(profile, "pg_admin_username", None) or None),
        reseller_profile_id=pid,
        bot_user_id=user_id,
        status=STATUS_ACTIVE,
    )


async def bind_reseller_profile_principal(
    session: AsyncSession,
    profile: ResellerProfile | None,
) -> OrgPrincipal | None:
    """Server-loaded ResellerProfile → its own Principal (depth 1 or 2).

    Auto-map of a *new* shop is always depth-1 under Owner. An existing
    depth-2 link (sub-representative shop) is returned as-is. Cookie
    ``org_principal_id`` / parent / depth / scope are never selectors.
    """
    if profile is None or not getattr(profile, "id", None):
        return None
    try:
        uid = int(getattr(profile, "user_id", 0) or 0)
    except (TypeError, ValueError):
        uid = 0
    if uid <= 0:
        return None
    principal = await map_reseller_profile_to_principal(session, profile)
    if principal is None or is_owner_principal(principal):
        return None
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        return None
    if depth not in (DEPTH_ONE, DEPTH_TWO):
        return None
    if int(principal.reseller_profile_id or 0) != int(profile.id):
        return None
    if int(principal.bot_user_id or 0) != uid:
        return None
    if str(principal.status) != STATUS_ACTIVE:
        return None
    return principal


async def resolve_pg_staff_principal(
    session: AsyncSession,
    staff: PgStaffAccess | int,
) -> OrgPrincipal | LegacyUnassigned:
    """Resolve pg_staff mapping without inventing a parent under Owner.

    If a principal row already exists (with a valid depth-1 parent), return it.
    Otherwise return LegacyUnassigned — do not create depth-2 under Owner.
    """
    if isinstance(staff, int):
        staff_id = int(staff)
        row = await session.get(PgStaffAccess, staff_id)
    else:
        row = staff
        staff_id = int(row.id) if row is not None else 0
    if staff_id <= 0:
        return LegacyUnassigned(pg_staff_id=None, reason="invalid_staff")

    existing = await get_principal_by_pg_staff(session, staff_id)
    if existing is None:
        return LegacyUnassigned(pg_staff_id=staff_id, reason="no_reliable_parent")

    # Existing row must already satisfy depth rules; do not reparent
    if existing.depth == DEPTH_TWO:
        parent = await get_principal(session, int(existing.parent_id or 0))
        if parent is None or parent.depth != DEPTH_ONE:
            return LegacyUnassigned(
                pg_staff_id=staff_id, reason="invalid_existing_parent"
            )
    elif existing.depth == DEPTH_OWNER:
        return LegacyUnassigned(pg_staff_id=staff_id, reason="staff_cannot_be_owner")
    return existing


async def list_principals_for_bot_user(
    session: AsyncSession, bot_user_id: int
) -> list[OrgPrincipal]:
    """All principals claiming ``bot_user_id`` (may be empty or ambiguous)."""
    try:
        uid = int(bot_user_id)
    except (TypeError, ValueError):
        return []
    if uid <= 0:
        return []
    result = await session.execute(
        select(OrgPrincipal).where(OrgPrincipal.bot_user_id == uid)
    )
    return list(result.scalars().all())


async def get_principal_by_bot_user(
    session: AsyncSession, bot_user_id: int
) -> OrgPrincipal | None:
    """Unique ``bot_user_id`` mapping, or None when missing/ambiguous (fail closed)."""
    rows = await list_principals_for_bot_user(session, bot_user_id)
    if len(rows) != 1:
        return None
    return rows[0]


async def resolve_org_principal_for_staff(
    session: AsyncSession,
    staff: dict | None,
) -> OrgPrincipal | None:
    """Server-side staff dict → OrgPrincipal. Never trusts client scope cookies.

    ``org_principal_id`` / depth / parent on the dict are ignored as selectors.
    Bind only via identity keys:

    - ``role=admin`` and ``web_owner=True`` → active Owner (not ``web_owner`` on other roles)
    - reseller → adapter / bot_user mapping
    - pg_staff → existing mapping or None if LegacyUnassigned
    - principal → ``web_identity_id`` only
    - bare ``role=admin`` → None (not Owner)
    """
    if not staff:
        return None

    role = str(staff.get("role") or "").strip()

    # Explicit web Owner flag is only valid on the web_admin.json admin session.
    # Reseller / pg_staff / principal cookies must not become Owner via this flag.
    if role == "admin" and staff.get("web_owner") is True:
        try:
            owner = await ensure_owner_principal(session)
        except OrgPrincipalError:
            return None
        return owner if is_owner_principal(owner) else None

    if role == "reseller":
        try:
            bot_uid = int(staff.get("bot_user_id") or 0)
        except (TypeError, ValueError):
            bot_uid = 0
        if bot_uid > 0:
            by_bot = await get_principal_by_bot_user(session, bot_uid)
            if by_bot is not None and str(by_bot.status) == STATUS_ACTIVE:
                return by_bot
        # Try reseller profile adapter when profile id known
        try:
            rpid = int(staff.get("reseller_profile_id") or 0)
        except (TypeError, ValueError):
            rpid = 0
        if rpid > 0:
            profile = await session.get(ResellerProfile, rpid)
            if profile is not None:
                return await map_reseller_profile_to_principal(session, profile)
        return None

    if role == "pg_staff":
        try:
            sid = int(staff.get("pg_staff_id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0:
            return None
        resolved = await resolve_pg_staff_principal(session, sid)
        if isinstance(resolved, LegacyUnassigned):
            return None
        if str(resolved.status) != STATUS_ACTIVE:
            return None
        return resolved

    if role == "principal":
        # Phase 2B/3C: resolve via web_identity_id only — never trust cookie principal_id.
        try:
            wid = int(staff.get("web_identity_id") or 0)
        except (TypeError, ValueError):
            wid = 0
        if wid <= 0:
            return None
        from app.db.models import OrgPrincipalWebIdentity

        identity = await session.get(OrgPrincipalWebIdentity, wid)
        if identity is None or not bool(identity.is_active):
            return None
        row = await get_principal(session, int(identity.principal_id))
        if row is None or str(row.status) != STATUS_ACTIVE:
            return None
        try:
            depth = int(row.depth)
        except (TypeError, ValueError):
            return None
        if depth == DEPTH_ONE:
            return row
        if depth == DEPTH_TWO:
            if row.parent_id is None:
                return None
            parent = await get_principal(session, int(row.parent_id))
            if parent is None or str(parent.status) != STATUS_ACTIVE:
                return None
            if int(parent.depth) != DEPTH_ONE:
                return None
            return row
        return None

    # role=admin alone is NOT Owner
    return None


def attach_org_principal_fields(
    staff: dict,
    principal: OrgPrincipal | None,
    *,
    visible_principal_ids: frozenset[int] | None = None,
) -> dict:
    """Return a copy of staff with server-side principal fields (not cookie-trusted)."""
    out = dict(staff)
    if principal is None:
        out.pop("org_principal_id", None)
        out.pop("org_parent_id", None)
        out.pop("org_depth", None)
        out.pop("org_status", None)
        out["org_visible_principal_ids"] = []
        out["web_owner"] = False
        return out
    out["org_principal_id"] = int(principal.id)
    out["org_parent_id"] = (
        int(principal.parent_id) if principal.parent_id is not None else None
    )
    out["org_depth"] = int(principal.depth)
    out["org_status"] = str(principal.status)
    if visible_principal_ids is not None:
        out["org_visible_principal_ids"] = sorted(int(x) for x in visible_principal_ids)
    if is_owner_principal(principal):
        out["web_owner"] = True
    return out


@dataclass(frozen=True)
class PrincipalPurgeResult:
    """Ids touched while removing a principal tree ahead of adapter deletes."""

    purged_principal_ids: tuple[int, ...]
    detached_reseller_profile_ids: tuple[int, ...]
    detached_pg_staff_ids: tuple[int, ...]


async def list_direct_child_principals(
    session: AsyncSession, parent_id: int
) -> list[OrgPrincipal]:
    result = await session.execute(
        select(OrgPrincipal)
        .where(OrgPrincipal.parent_id == int(parent_id))
        .order_by(OrgPrincipal.id.asc())
    )
    return list(result.scalars().all())


async def _clear_owner_principal_refs(session: AsyncSession, principal_id: int) -> None:
    """Null business-resource FKs so principal rows can be deleted safely."""
    pid = int(principal_id)
    for model in (BotUser, Order, Ticket, UserService):
        col = getattr(model, "owner_principal_id", None)
        if col is None:
            continue
        await session.execute(update(model).where(col == pid).values(owner_principal_id=None))


async def _delete_principal_side_rows(session: AsyncSession, principal_id: int) -> None:
    pid = int(principal_id)
    identities = (
        await session.execute(
            select(OrgPrincipalWebIdentity).where(
                OrgPrincipalWebIdentity.principal_id == pid
            )
        )
    ).scalars().all()
    for identity in identities:
        await session.delete(identity)

    provisions = (
        await session.execute(
            select(OrgPrincipalProvision).where(
                or_(
                    OrgPrincipalProvision.principal_id == pid,
                    OrgPrincipalProvision.created_by_principal_id == pid,
                )
            )
        )
    ).scalars().all()
    for prov in provisions:
        await session.delete(prov)


def _invalidate_pg_principal_cache(principal_id: int) -> None:
    try:
        from app.services.pasarguard import invalidate_pg_principal_cache

        invalidate_pg_principal_cache(int(principal_id))
    except Exception:
        log.error(
            "Failed to invalidate PG cache after principal purge id=%s",
            int(principal_id),
        )


async def _purge_principal_node(session: AsyncSession, row: OrgPrincipal) -> None:
    """Delete one principal after dependents are gone. Never deletes Owner."""
    if is_owner_principal(row) or (
        int(row.depth) == DEPTH_OWNER and row.parent_id is None
    ):
        raise OrgPrincipalError("cannot purge Owner principal")

    pid = int(row.id)
    await _clear_owner_principal_refs(session, pid)
    await _delete_principal_side_rows(session, pid)

    # Detach adapter FKs so ResellerProfile / PgStaffAccess can be deleted next.
    row.reseller_profile_id = None
    row.pg_staff_id = None
    row.bot_user_id = None
    await session.flush()

    await session.delete(row)
    await session.flush()
    _invalidate_pg_principal_cache(pid)


async def purge_org_principal_tree(
    session: AsyncSession,
    root: OrgPrincipal,
) -> PrincipalPurgeResult:
    """Delete ``root`` and all descendants (children first).

    Clears ``owner_principal_id`` refs, web identities, and provision ledger
    rows. Nulls adapter FKs on each principal before delete so
    ``ResellerProfile`` / ``PgStaffAccess`` rows can be removed afterward.

    Does **not** delete ResellerProfile, PgStaffAccess, or BotUser rows.
    Refuses Owner. Safe for revoke/delete adapter paths.
    """
    if root is None or not getattr(root, "id", None):
        return PrincipalPurgeResult((), (), ())
    if is_owner_principal(root) or (
        int(root.depth) == DEPTH_OWNER and root.parent_id is None
    ):
        raise OrgPrincipalError("cannot purge Owner principal")

    purged: list[int] = []
    detached_profiles: list[int] = []
    detached_staff: list[int] = []

    async def _walk(node: OrgPrincipal) -> None:
        children = await list_direct_child_principals(session, int(node.id))
        for child in children:
            await _walk(child)

        rpid = int(node.reseller_profile_id or 0)
        if rpid > 0:
            detached_profiles.append(rpid)
        sid = int(node.pg_staff_id or 0)
        if sid > 0:
            detached_staff.append(sid)
        purged.append(int(node.id))
        await _purge_principal_node(session, node)

    await _walk(root)
    return PrincipalPurgeResult(
        purged_principal_ids=tuple(purged),
        detached_reseller_profile_ids=tuple(dict.fromkeys(detached_profiles)),
        detached_pg_staff_ids=tuple(dict.fromkeys(detached_staff)),
    )


async def purge_principal_for_reseller_profile(
    session: AsyncSession, reseller_profile_id: int
) -> PrincipalPurgeResult:
    """Purge the org principal tree linked to a ResellerProfile (if any)."""
    try:
        pid = int(reseller_profile_id)
    except (TypeError, ValueError):
        return PrincipalPurgeResult((), (), ())
    if pid <= 0:
        return PrincipalPurgeResult((), (), ())
    root = await get_principal_by_reseller_profile(session, pid)
    if root is None:
        return PrincipalPurgeResult((), (), ())
    return await purge_org_principal_tree(session, root)


async def purge_principal_for_pg_staff(
    session: AsyncSession, pg_staff_id: int
) -> PrincipalPurgeResult:
    """Purge the org principal tree linked to a PgStaffAccess row (if any)."""
    try:
        sid = int(pg_staff_id)
    except (TypeError, ValueError):
        return PrincipalPurgeResult((), (), ())
    if sid <= 0:
        return PrincipalPurgeResult((), (), ())
    root = await get_principal_by_pg_staff(session, sid)
    if root is None:
        return PrincipalPurgeResult((), (), ())
    return await purge_org_principal_tree(session, root)
