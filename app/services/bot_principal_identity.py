"""Phase 4A / 5B — Bot → OrgPrincipal identity bridge (same Principal as Web).

Telegram identity resolves to OrgPrincipal through trusted server-side mappings
only. Never uses client callback fields, role names, or cookie principal ids.

Resolution order:
  1. Shop/reseller bot token → that shop's Principal (L1 or L2; L2 requires
     an active depth-1 parent). Customers are not operators.
  2. Platform (Owner) bot: ADMIN_IDS → singleton Owner Principal
  3. Platform bot: ResellerProfile on db_user → depth-1 Principal only
  4. Sub-Representative never operates on the Owner bot (no telegram fallback)

Identity + AuthzContext only — PG gates live in the 4B–4E pilots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, OrgPrincipal, ResellerProfile
from app.services.authz import AuthzContext, authz_from_staff
from app.services.org_principals import (
    DEPTH_ONE,
    DEPTH_TWO,
    STATUS_ACTIVE,
    bind_reseller_profile_principal,
    ensure_owner_principal,
    get_principal,
    is_owner_principal,
    list_principals_for_bot_user,
)
from app.services.org_scope import visible_principal_ids


@dataclass(frozen=True)
class BotPrincipalResolution:
    """Resolved Bot operator identity bound to an OrgPrincipal."""

    principal: OrgPrincipal
    staff: dict[str, Any]
    authz: AuthzContext
    channel: str  # "owner" | "reseller_l1" | "principal_l2"


def bot_pg_family_resolution_ok(resolution: BotPrincipalResolution | None) -> bool:
    """Whether a Bot PG-family gate may run for this resolved actor.

    Owner and active L1 keep existing behavior. L2 is allowed when:

    - depth == 2, active, channel principal_l2
    - AuthzContext scope is exactly ``{self}`` (no parent/sibling)
    - shop id is this Principal's own shop (never parent/sibling/Owner bot)
    - not Owner / ``pg_is_owner``
    """
    if resolution is None:
        return False
    principal = resolution.principal
    if is_owner_principal(principal):
        return True
    if str(getattr(principal, "status", "")) != STATUS_ACTIVE:
        return False
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        return False
    if depth == DEPTH_ONE:
        return True
    if depth != DEPTH_TWO:
        return False
    if resolution.channel != "principal_l2":
        return False
    ctx = resolution.authz
    try:
        pid = int(principal.id)
        ctx_pid = int(ctx.principal_id or 0)
        parent_id = int(ctx.parent_id or 0)
    except (TypeError, ValueError):
        return False
    if ctx.depth != DEPTH_TWO or ctx_pid != pid or parent_id <= 0:
        return False
    if str(ctx.principal_status or "") != STATUS_ACTIVE:
        return False
    if ctx.visible_principal_ids != frozenset({pid}):
        return False
    staff = resolution.staff or {}
    if staff.get("pg_is_owner") or staff.get("web_owner"):
        return False
    from app.services.platform_identity import is_explicit_owner_staff
    from app.services.shop_scope import shop_owner_id

    if is_explicit_owner_staff(staff):
        return False
    rid = shop_owner_id(staff)
    if not rid:
        return False
    try:
        own = int(principal.bot_user_id or 0)
    except (TypeError, ValueError):
        own = 0
    return own == int(rid) and own > 0


def callback_carries_principal_tamper(callback_data: str | None) -> bool:
    """True when callback payload tries to inject hierarchy identity fields."""
    if not callback_data:
        return False
    lowered = str(callback_data).lower()
    for needle in (
        "org_principal_id",
        "org_principal:",
        "web_owner:",
        "org_depth:",
        "org_parent_id",
    ):
        if needle in lowered:
            return True
    return False


def _admin_ids_set(admin_ids: frozenset[int] | set[int] | None) -> frozenset[int]:
    raw: Iterable[int]
    if admin_ids is not None:
        raw = admin_ids
    else:
        try:
            from app.config import get_settings

            raw = get_settings().admin_ids or ()
        except Exception:
            return frozenset()
    out: set[int] = set()
    for x in raw:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.add(n)
    return frozenset(out)


def _telegram_id(user: BotUser | None) -> int:
    if user is None:
        return 0
    try:
        return int(getattr(user, "telegram_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def shop_bot_actor_is_operator(
    session: AsyncSession | None,
    db_user: BotUser | None,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None = None,
) -> bool:
    """True when this Telegram user may run this Representative's own-bot ops.

    Customers on a shop bot are not operators. Owner bot is not a shop bot.
    """
    if not is_reseller_bot:
        return True
    if session is None or db_user is None:
        return False
    from app.services.reseller_access import resolve_reseller_owner_id

    owner_id = await resolve_reseller_owner_id(
        session,
        db_user,
        is_reseller_bot=True,
        reseller_owner_id=reseller_owner_id,
    )
    try:
        return int(owner_id or 0) > 0
    except (TypeError, ValueError):
        return False


async def resolve_bot_pg_family_identity(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None,
    is_reseller_bot: bool,
    reseller_profile_id: int | None,
    reseller_owner_id: int | None,
    identity_tamper: bool,
) -> BotPrincipalResolution | str:
    """Identity for the platform PG-user / object / catalog family.

    Shop bots are isolated from this family except a bound L2 operator on
    their own bot (channel ``principal_l2``). An L1 shop owner on a shop
    token stays isolated. Callback identity tamper always denies.
    """
    if session is None or db_user is None:
        return "unauthenticated"
    if identity_tamper:
        return "identity_tamper"
    resolution = await resolve_bot_principal_bridge(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        spoof_org_principal_id=None,
    )
    if is_reseller_bot:
        if resolution is None or resolution.channel != "principal_l2":
            return "shop_bot_isolated"
    if resolution is None or not bot_pg_family_resolution_ok(resolution):
        return "missing_principal"
    return resolution


def _validate_active_shop_rep(
    principal: OrgPrincipal | None,
    *,
    expected_bot_user_id: int | None = None,
    expected_reseller_profile_id: int | None = None,
) -> OrgPrincipal | None:
    """Active depth-1 or depth-2 shop-linked Principal."""
    if principal is None or is_owner_principal(principal):
        return None
    if str(principal.status) != STATUS_ACTIVE:
        return None
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        return None
    if depth not in (DEPTH_ONE, DEPTH_TWO):
        return None
    if expected_bot_user_id is not None:
        try:
            got = int(principal.bot_user_id or 0)
        except (TypeError, ValueError):
            got = 0
        if got != int(expected_bot_user_id):
            return None
    if expected_reseller_profile_id is not None:
        try:
            got = int(principal.reseller_profile_id or 0)
        except (TypeError, ValueError):
            got = 0
        if got != int(expected_reseller_profile_id):
            return None
    parent_id = getattr(principal, "parent_id", None)
    if parent_id is None:
        return None
    return principal


async def _active_l1_parent(
    session: AsyncSession, principal: OrgPrincipal
) -> bool:
    parent_id = getattr(principal, "parent_id", None)
    if parent_id is None:
        return False
    parent = await get_principal(session, int(parent_id))
    if parent is None or str(parent.status) != STATUS_ACTIVE:
        return False
    try:
        return int(getattr(parent, "depth", -1) or -1) == DEPTH_ONE
    except (TypeError, ValueError):
        return False


async def _active_shop_rep(
    session: AsyncSession,
    principal: OrgPrincipal | None,
    *,
    expected_bot_user_id: int | None = None,
    expected_reseller_profile_id: int | None = None,
) -> OrgPrincipal | None:
    """Shop-linked Principal that may operate; L2 also requires active L1 parent."""
    got = _validate_active_shop_rep(
        principal,
        expected_bot_user_id=expected_bot_user_id,
        expected_reseller_profile_id=expected_reseller_profile_id,
    )
    if got is None:
        return None
    try:
        depth = int(got.depth)
    except (TypeError, ValueError):
        return None
    if depth == DEPTH_TWO and not await _active_l1_parent(session, got):
        return None
    return got


async def _validate_active_l2(
    session: AsyncSession,
    principal: OrgPrincipal | None,
    *,
    expected_bot_user_id: int,
    db_user: BotUser | None,
    admin_ids: frozenset[int] | set[int] | None,
) -> OrgPrincipal | None:
    """Active depth-2 with unique bot_user bind and active L1 parent."""
    if principal is None or is_owner_principal(principal):
        return None
    if str(principal.status) != STATUS_ACTIVE:
        return None
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        return None
    if depth != DEPTH_TWO:
        return None
    try:
        got = int(principal.bot_user_id or 0)
    except (TypeError, ValueError):
        got = 0
    if got != int(expected_bot_user_id) or got <= 0:
        return None
    parent_id = getattr(principal, "parent_id", None)
    if parent_id is None:
        return None
    parent = await get_principal(session, int(parent_id))
    if (
        parent is None
        or str(parent.status) != STATUS_ACTIVE
        or int(getattr(parent, "depth", -1) or -1) != DEPTH_ONE
    ):
        return None
    tid = _telegram_id(db_user)
    if tid <= 0 or tid in _admin_ids_set(admin_ids):
        return None
    from app.services.resellers import get_reseller_profile

    if db_user is not None:
        profile = await get_reseller_profile(session, int(db_user.id))
        if profile is not None and bool(profile.is_active):
            return None
    return principal


async def _owner_bot_principal(
    session: AsyncSession,
    *,
    db_user: BotUser | None,
    is_reseller_bot: bool,
    admin_ids: frozenset[int] | None,
) -> OrgPrincipal | None:
    if is_reseller_bot or db_user is None:
        return None
    tid = _telegram_id(db_user)
    if tid <= 0 or tid not in _admin_ids_set(admin_ids):
        return None
    try:
        owner = await ensure_owner_principal(session)
    except Exception:
        return None
    if not is_owner_principal(owner) or str(owner.status) != STATUS_ACTIVE:
        return None
    return owner


async def _reseller_shop_bot_principal(
    session: AsyncSession,
    *,
    reseller_profile_id: int | None,
    reseller_owner_id: int | None,
) -> OrgPrincipal | None:
    if reseller_profile_id is None:
        return None
    try:
        pid = int(reseller_profile_id)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    profile = await session.get(ResellerProfile, pid)
    if profile is None or not bool(profile.is_active):
        return None
    if reseller_owner_id is not None:
        try:
            if int(profile.user_id) != int(reseller_owner_id):
                return None
        except (TypeError, ValueError):
            return None
    bound = await bind_reseller_profile_principal(session, profile)
    return await _active_shop_rep(
        session,
        bound,
        expected_bot_user_id=int(profile.user_id),
        expected_reseller_profile_id=int(profile.id),
    )


async def _reseller_user_bot_principal(
    session: AsyncSession,
    *,
    db_user: BotUser | None,
) -> OrgPrincipal | None:
    if db_user is None:
        return None
    try:
        uid = int(db_user.id)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, uid)
    if profile is None or not bool(profile.is_active):
        return None
    bound = await bind_reseller_profile_principal(session, profile)
    validated = await _active_shop_rep(
        session,
        bound,
        expected_bot_user_id=int(profile.user_id),
        expected_reseller_profile_id=int(profile.id),
    )
    if validated is None:
        return None
    try:
        if int(validated.depth) != DEPTH_ONE:
            return None
    except (TypeError, ValueError):
        return None
    return validated


async def _l2_user_bot_principal(
    session: AsyncSession,
    *,
    db_user: BotUser | None,
    admin_ids: frozenset[int] | set[int] | None,
) -> OrgPrincipal | None:
    if db_user is None:
        return None
    try:
        uid = int(db_user.id)
    except (TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    rows = await list_principals_for_bot_user(session, uid)
    if len(rows) != 1:
        return None
    return await _validate_active_l2(
        session,
        rows[0],
        expected_bot_user_id=uid,
        db_user=db_user,
        admin_ids=admin_ids,
    )


async def resolve_bot_org_principal(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
    admin_ids: frozenset[int] | None = None,
    spoof_org_principal_id: int | None = None,
) -> OrgPrincipal | None:
    """Trusted Telegram / token context → OrgPrincipal (or None).

    ``spoof_org_principal_id`` simulates callback/cookie injection in tests;
    it is never used for resolution.
    """
    _ = spoof_org_principal_id
    if session is None:
        return None

    if is_reseller_bot:
        return await _reseller_shop_bot_principal(
            session,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
        )

    owner = await _owner_bot_principal(
        session,
        db_user=db_user,
        is_reseller_bot=False,
        admin_ids=admin_ids,
    )
    if owner is not None:
        return owner

    l1 = await _reseller_user_bot_principal(session, db_user=db_user)
    if l1 is not None:
        return l1

    # Sub-Representative never operates on the Owner bot. Own shop bot only.
    return None


async def _staff_for_owner(
    session: AsyncSession,
    principal: OrgPrincipal,
    *,
    db_user: BotUser | None,
) -> dict[str, Any]:
    visible = await visible_principal_ids(session, principal)
    uname = None
    if db_user is not None:
        uname = getattr(db_user, "username", None) or str(_telegram_id(db_user))
    staff: dict[str, Any] = {
        "role": "admin",
        "web_owner": True,
        "username": uname or "owner",
    }
    if db_user is not None:
        try:
            staff["bot_user_id"] = int(db_user.id)
        except (TypeError, ValueError):
            pass
    from app.services.org_principals import attach_org_principal_fields
    from app.services.pg_access import enrich_platform_admin_staff

    staff = attach_org_principal_fields(
        staff, principal, visible_principal_ids=visible
    )
    staff = await enrich_platform_admin_staff(staff)
    return staff


async def _staff_for_reseller_l1(
    session: AsyncSession,
    principal: OrgPrincipal,
    *,
    profile: ResellerProfile,
) -> dict[str, Any]:
    from app.services.authz import resolve_shop_permissions_from_profile
    from app.services.org_principals import attach_org_principal_fields
    from app.services.pg_access import enrich_staff_pg_from_role, resolve_acl_from_client

    visible = await visible_principal_ids(session, principal)
    perms = resolve_shop_permissions_from_profile(profile) or []
    staff: dict[str, Any] = {
        "role": "reseller",
        "bot_user_id": int(profile.user_id),
        "reseller_profile_id": int(profile.id),
        "permissions": list(perms),
    }
    if profile.pg_admin_username:
        staff["pg_admin_username"] = profile.pg_admin_username
    pg_client = None
    if profile.pg_admin_username:
        try:
            from app.services.pasarguard import get_pg_for_reseller

            pg_client = await get_pg_for_reseller(session, int(profile.user_id))
        except Exception:
            pg_client = None
    if pg_client is not None:
        features, role, _admin = await resolve_acl_from_client(
            pg_client, username=profile.pg_admin_username
        )
        staff = enrich_staff_pg_from_role(staff, features, role)
        if isinstance(role, dict) and role.get("id") is not None:
            try:
                staff["pg_role_id"] = int(role["id"])
            except (TypeError, ValueError):
                pass
    elif profile.pg_role_id:
        staff["pg_role_id"] = int(profile.pg_role_id)
    staff["pg_credentials_ready"] = bool(
        profile.pg_admin_password_enc
        or getattr(profile, "pg_api_key_enc", None)
        or principal.pg_password_enc
        or getattr(principal, "pg_api_key_enc", None)
    )
    return attach_org_principal_fields(
        staff, principal, visible_principal_ids=visible
    )


async def _staff_for_principal_l2(
    session: AsyncSession,
    principal: OrgPrincipal,
    *,
    db_user: BotUser | None,
) -> dict[str, Any] | None:
    """Web-shaped staff for a resolved L2 Bot operator (same AuthzContext as Web L2)."""
    from app.services.org_principals import attach_org_principal_fields
    from app.services.pg_access import enrich_staff_pg_from_role, resolve_acl_from_client
    from app.services.principal_pg_authz import apply_level1_pg_local_safety
    from app.services.principal_web_identity import ROLE_PRINCIPAL

    visible = await visible_principal_ids(session, principal)
    try:
        self_id = int(principal.id)
    except (TypeError, ValueError):
        return None
    if not visible or self_id not in visible:
        return None

    uname = None
    if db_user is not None:
        uname = getattr(db_user, "username", None) or str(_telegram_id(db_user))
    staff: dict[str, Any] = {
        "role": ROLE_PRINCIPAL,
        "web_owner": False,
        "username": uname or (principal.pg_username or "l2"),
        "permissions": [],
        "pg_admin_username": (principal.pg_username or "").strip() or None,
    }
    if db_user is not None:
        try:
            staff["bot_user_id"] = int(db_user.id)
        except (TypeError, ValueError):
            pass
    elif principal.bot_user_id:
        staff["bot_user_id"] = int(principal.bot_user_id)

    pg_uname = staff.get("pg_admin_username")
    role = None
    if not pg_uname:
        staff["pg_permissions"] = []
        staff["pg_actions"] = {}
        staff["pg_user_actions"] = {}
        staff["pg_writes"] = {}
        staff["pg_credentials_ready"] = False
        staff["pg_capabilities_ok"] = False
    else:
        pg_client = None
        try:
            from app.services.pasarguard import get_pg_for_principal

            pg_client = await get_pg_for_principal(
                session, principal_id=int(principal.id)
            )
        except Exception:
            pg_client = None
        if pg_client is not None:
            try:
                features, role, _admin = await resolve_acl_from_client(
                    pg_client, username=str(pg_uname)
                )
            except Exception:
                features, role = [], None
            if not features and role is None:
                staff["pg_permissions"] = []
                staff["pg_actions"] = {}
                staff["pg_user_actions"] = {}
                staff["pg_writes"] = {}
                staff["pg_capabilities_ok"] = False
            else:
                staff = enrich_staff_pg_from_role(staff, features, role)
                staff["pg_capabilities_ok"] = True
                if isinstance(role, dict) and role.get("id") is not None:
                    try:
                        staff["pg_role_id"] = int(role["id"])
                    except (TypeError, ValueError):
                        pass
        else:
            staff["pg_permissions"] = []
            staff["pg_actions"] = {}
            staff["pg_user_actions"] = {}
            staff["pg_writes"] = {}
            staff["pg_capabilities_ok"] = False
        staff["pg_credentials_ready"] = bool(
            (principal.pg_password_enc or "").strip()
            or (getattr(principal, "pg_api_key_enc", None) or "").strip()
        )

    staff = apply_level1_pg_local_safety(
        staff, role if isinstance(role, dict) else None
    )
    staff = attach_org_principal_fields(
        staff, principal, visible_principal_ids=visible
    )
    staff["web_owner"] = False
    staff["pg_is_owner"] = False
    return staff


async def bot_staff_from_org_principal(
    session: AsyncSession,
    principal: OrgPrincipal,
    *,
    db_user: BotUser | None = None,
    reseller_profile_id: int | None = None,
) -> dict[str, Any] | None:
    """Build Web-shaped staff dict from a resolved OrgPrincipal."""
    if is_owner_principal(principal):
        return await _staff_for_owner(session, principal, db_user=db_user)

    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        return None

    if depth == DEPTH_TWO:
        profile = await _linked_shop_profile(
            session, principal, reseller_profile_id=reseller_profile_id, db_user=db_user
        )
        if profile is None:
            # Shop-less L2 has no independent Bot — do not fall back to Owner bot.
            return None
        validated = await _active_shop_rep(
            session,
            principal,
            expected_bot_user_id=int(profile.user_id),
            expected_reseller_profile_id=int(profile.id),
        )
        if validated is None:
            return None
        staff = await _staff_for_principal_l2(
            session, validated, db_user=db_user
        )
        if staff is None:
            return None
        staff["reseller_profile_id"] = int(profile.id)
        staff["bot_user_id"] = int(profile.user_id)
        staff["pg_credentials_ready"] = bool(
            profile.pg_admin_password_enc
            or getattr(profile, "pg_api_key_enc", None)
            or principal.pg_password_enc
            or getattr(principal, "pg_api_key_enc", None)
        )
        staff["web_owner"] = False
        staff["pg_is_owner"] = False
        return staff

    if depth != DEPTH_ONE:
        return None

    profile = await _linked_shop_profile(
        session, principal, reseller_profile_id=reseller_profile_id, db_user=db_user
    )
    if profile is None or not bool(profile.is_active):
        return None
    validated = await _active_shop_rep(
        session,
        principal,
        expected_bot_user_id=int(profile.user_id),
        expected_reseller_profile_id=int(profile.id),
    )
    if validated is None:
        return None
    return await _staff_for_reseller_l1(session, validated, profile=profile)


async def _linked_shop_profile(
    session: AsyncSession,
    principal: OrgPrincipal,
    *,
    reseller_profile_id: int | None = None,
    db_user: BotUser | None = None,
) -> ResellerProfile | None:
    profile: ResellerProfile | None = None
    if reseller_profile_id is not None:
        profile = await session.get(ResellerProfile, int(reseller_profile_id))
    elif principal.reseller_profile_id is not None:
        profile = await session.get(ResellerProfile, int(principal.reseller_profile_id))
    elif db_user is not None:
        from app.services.resellers import get_reseller_profile

        profile = await get_reseller_profile(session, int(db_user.id))
    if profile is None or not bool(profile.is_active):
        return None
    return profile


async def resolve_bot_principal_bridge(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
    admin_ids: frozenset[int] | None = None,
    spoof_org_principal_id: int | None = None,
) -> BotPrincipalResolution | None:
    """Full Bot identity bridge: Principal + staff dict + AuthzContext."""
    principal = await resolve_bot_org_principal(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        admin_ids=admin_ids,
        spoof_org_principal_id=spoof_org_principal_id,
    )
    if principal is None or session is None:
        return None

    if is_owner_principal(principal):
        staff = await bot_staff_from_org_principal(
            session, principal, db_user=db_user
        )
        if staff is None:
            return None
        return BotPrincipalResolution(
            principal=principal,
            staff=staff,
            authz=authz_from_staff(staff),
            channel="owner",
        )

    staff = await bot_staff_from_org_principal(
        session,
        principal,
        db_user=db_user,
        reseller_profile_id=reseller_profile_id,
    )
    if staff is None:
        return None
    try:
        depth = int(principal.depth)
    except (TypeError, ValueError):
        depth = -1
    channel = "principal_l2" if depth == DEPTH_TWO else "reseller_l1"
    return BotPrincipalResolution(
        principal=principal,
        staff=staff,
        authz=authz_from_staff(staff),
        channel=channel,
    )


async def bot_pg_client_for_resolution(
    session: AsyncSession,
    resolution: BotPrincipalResolution,
):
    """Return (pg_client, as_owner) — never Owner credentials for L1/L2."""
    from app.services.pasarguard import (
        PasarGuardError,
        get_pg,
        get_pg_for_principal,
        get_pg_for_reseller,
    )
    from app.services.platform_identity import is_explicit_owner_staff
    from app.services.shop_scope import shop_owner_id

    staff = resolution.staff
    if is_explicit_owner_staff(staff):
        return get_pg(), bool(staff.get("pg_is_owner"))
    try:
        depth = int(resolution.principal.depth)
    except (TypeError, ValueError):
        depth = -1
    if depth == DEPTH_TWO or resolution.channel == "principal_l2":
        return (
            await get_pg_for_principal(
                session, principal_id=int(resolution.principal.id)
            ),
            False,
        )
    rid = shop_owner_id(staff)
    if rid:
        return await get_pg_for_reseller(session, int(rid)), False
    raise PasarGuardError(
        "کلاینت پاسارگارد برای این Principal Bot در دسترس نیست"
    )


async def assert_l2_would_deny_if_disabled_parent(
    session: AsyncSession,
    principal_id: int,
) -> bool:
    """Future L2 guard: inactive L1 parent → deny (scope empty)."""
    row = await get_principal(session, int(principal_id))
    if row is None:
        return True
    try:
        depth = int(row.depth)
    except (TypeError, ValueError):
        return True
    if depth != 2:
        return False
    parent_id = getattr(row, "parent_id", None)
    if parent_id is None:
        return True
    parent = await get_principal(session, int(parent_id))
    if parent is None or str(parent.status) != STATUS_ACTIVE:
        return True
    if int(parent.depth) != DEPTH_ONE:
        return True
    visible = await visible_principal_ids(session, row)
    return len(visible) == 0
