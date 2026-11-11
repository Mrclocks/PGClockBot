"""«دسترسی به پاسارگارد»: whether a reseller shop may use the PasarGuard panel itself.

When the switch is off for a shop, every staff session built for it (web reseller,
level-1/level-2 principal linked to the shop, and the bot) carries no PasarGuard
capability. Menu, routes and actions read those capabilities, so they close together.
The shop keeps selling: shop operations use the stored PasarGuard credential, not the
staff session.

Closing access also replaces the PasarGuard admin password, so a password the reseller
already knows (web and panel logins used to share one secret) stops working. The new
password is saved before the panel call and reused by every retry, so a timeout never
leaves the shop with a password nobody stored.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrgPrincipal, ResellerProfile
from app.py_compat import StrEnum
from app.services.credential_policy import generate_compliant_password
from app.services.org_principals import get_principal_by_reseller_profile
from app.services.pasarguard import (
    PasarGuardError,
    get_pg,
    invalidate_pg_principal_cache,
    invalidate_pg_reseller_cache,
)
from app.services.secret_box import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

ROTATION_PASSWORD_LENGTH = 20
PANEL_BLOCKED_FLAG = "pg_panel_blocked"

# Session fields that grant PasarGuard pages or actions (see ``authz.AuthzContext``).
_EMPTY_CAPABILITIES: dict[str, Any] = {
    "pg_permissions": [],
    "pg_actions": {},
    "pg_user_actions": {},
    "pg_writes": {},
}

ROTATION_FAILED_MESSAGE = (
    "دسترسی به پاسارگارد بسته شد، ولی تغییر رمز ادمین پاسارگارد تأیید نشد؛ "
    "از همین صفحه «تکرار تغییر رمز پاسارگارد» را بزنید"
)


API_KEY_STILL_VALID_MESSAGE = (
    "دسترسی به پاسارگارد بسته شد، ولی این فروشگاه با کلید API کار می‌کند و تغییر رمز "
    "آن کلید را باطل نمی‌کند؛ اگر نماینده این کلید را دیده است، آن را در پاسارگارد باطل "
    "کنید و کلید تازه را در همین صفحه ثبت کنید"
)


class RotationOutcome(StrEnum):
    DONE = "done"
    PENDING = "pending"
    NOT_NEEDED = "not_needed"


@dataclass(frozen=True, slots=True)
class AccessChange:
    previous: bool
    allowed: bool
    rotation: RotationOutcome

    @property
    def changed(self) -> bool:
        return self.previous != self.allowed


def pg_panel_allowed(profile: ResellerProfile | None) -> bool:
    """True unless this shop's «دسترسی به پاسارگارد» switch is off."""
    if profile is None:
        return True
    value = getattr(profile, "allow_pg_panel_access", True)
    return True if value is None else bool(value)


def revoke_pg_panel_capabilities(staff: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``staff`` with every PasarGuard page and action capability removed."""
    out = dict(staff)
    for key, empty in _EMPTY_CAPABILITIES.items():
        out[key] = type(empty)()
    out.pop("pg_role", None)
    out["pg_capabilities_ok"] = False
    out["pg_is_owner"] = False
    out[PANEL_BLOCKED_FLAG] = True
    return out


def apply_pg_panel_access(
    staff: Mapping[str, Any], profile: ResellerProfile | None
) -> dict[str, Any]:
    """Clamp a staff session built for ``profile``'s shop to the access switch."""
    if pg_panel_allowed(profile):
        out = dict(staff)
        out.pop(PANEL_BLOCKED_FLAG, None)
        return out
    return revoke_pg_panel_capabilities(staff)


def is_pg_panel_blocked(staff: Mapping[str, Any] | None) -> bool:
    return bool((staff or {}).get(PANEL_BLOCKED_FLAG))


def pg_password_follows_web(profile: ResellerProfile | None) -> bool:
    """Whether a new web panel password may also become the PasarGuard password."""
    return pg_panel_allowed(profile)


def may_share_pg_credentials(*, allow_access: bool, share_panel_url: bool) -> bool:
    """The PasarGuard address and password are sent only while access is open."""
    return bool(allow_access) and bool(share_panel_url)


async def _password_holder(
    session: AsyncSession, profile: ResellerProfile
) -> OrgPrincipal | ResellerProfile:
    """Row whose ``*_password_enc`` shop operations log in with (mirrors ``get_pg_for_reseller``)."""
    linked = await get_principal_by_reseller_profile(session, int(profile.id))
    if (
        linked is not None
        and str(getattr(linked, "status", "") or "") == "active"
        and int(getattr(linked, "depth", -1) or -1) in (1, 2)
        and (linked.pg_password_enc or "").strip()
    ):
        return linked
    return profile


async def _claim_rotation_secret(session: AsyncSession, profile: ResellerProfile) -> str:
    """Save one new password for this shop, or return the one an earlier attempt saved."""
    candidate = encrypt_secret(generate_compliant_password(ROTATION_PASSWORD_LENGTH))
    if not candidate:
        raise ValueError("رمزگذاری رمز تازهٔ پاسارگارد ناموفق بود؛ دوباره تلاش کنید")
    await session.execute(
        update(ResellerProfile)
        .where(
            ResellerProfile.id == int(profile.id),
            ResellerProfile.pg_password_rotation_enc.is_(None),
        )
        .values(pg_password_rotation_enc=candidate)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(profile)
    saved = profile.pg_password_rotation_enc or ""
    if not saved:
        raise ValueError("ذخیرهٔ رمز تازهٔ پاسارگارد ناموفق بود؛ دوباره تلاش کنید")
    return saved


def _drop_cached_clients(profile: ResellerProfile, holder: object) -> None:
    try:
        invalidate_pg_reseller_cache(int(profile.user_id))
        if isinstance(holder, OrgPrincipal):
            invalidate_pg_principal_cache(int(holder.id))
    except (TypeError, ValueError):
        logger.debug("pg client cache drop skipped profile=%s", profile.id, exc_info=True)


async def finish_pg_password_rotation(
    session: AsyncSession, profile: ResellerProfile
) -> RotationOutcome:
    """Send the saved new password to PasarGuard; on success make it the stored one.

    Setting the same password again is harmless, so any failure keeps the saved secret
    for the next attempt instead of guessing whether the panel applied it.
    """
    username = (profile.pg_admin_username or "").strip()
    pending = (profile.pg_password_rotation_enc or "").strip()
    if not username or not pending:
        return RotationOutcome.NOT_NEEDED
    password = decrypt_secret(pending)
    if not password:
        logger.warning("pg password rotation secret unreadable profile=%s", profile.id)
        return RotationOutcome.PENDING
    try:
        await get_pg().modify_admin(username, {"password": password})
    except (PasarGuardError, httpx.HTTPError, OSError):
        logger.warning("pg password rotation not confirmed profile=%s", profile.id, exc_info=True)
        return RotationOutcome.PENDING
    holder = await _password_holder(session, profile)
    if isinstance(holder, OrgPrincipal):
        holder.pg_password_enc = pending
    else:
        profile.pg_admin_password_enc = pending
    profile.pg_password_rotation_enc = None
    await session.commit()
    _drop_cached_clients(profile, holder)
    return RotationOutcome.DONE


async def shop_uses_pg_api_key(session: AsyncSession, profile: ResellerProfile) -> bool:
    """Whether shop operations log in with a stored API key, which a password change leaves valid."""
    if (getattr(profile, "pg_api_key_enc", None) or "").strip():
        return True
    linked = await get_principal_by_reseller_profile(session, int(profile.id))
    return linked is not None and bool((getattr(linked, "pg_api_key_enc", None) or "").strip())


async def close_access_warning(
    session: AsyncSession, profile: ResellerProfile, change: AccessChange
) -> str | None:
    """What the owner must still do after closing access, if anything."""
    if change.rotation == RotationOutcome.PENDING:
        return ROTATION_FAILED_MESSAGE
    if change.changed and await shop_uses_pg_api_key(session, profile):
        return API_KEY_STILL_VALID_MESSAGE
    return None


def open_pg_panel_access(profile: ResellerProfile) -> None:
    """Re-open access in the caller's transaction; a pending password change is dropped."""
    profile.allow_pg_panel_access = True
    profile.pg_password_rotation_enc = None


async def set_pg_panel_access(
    session: AsyncSession, profile: ResellerProfile, allowed: bool
) -> AccessChange:
    """Open or close PasarGuard access for a shop; closing replaces the panel password."""
    previous = pg_panel_allowed(profile)
    target = bool(allowed)
    if previous == target:
        return AccessChange(previous, target, RotationOutcome.NOT_NEEDED)
    if target:
        open_pg_panel_access(profile)
        await session.commit()
        return AccessChange(previous, target, RotationOutcome.NOT_NEEDED)
    profile.allow_pg_panel_access = False
    await session.commit()
    if not (profile.pg_admin_username or "").strip():
        return AccessChange(previous, target, RotationOutcome.NOT_NEEDED)
    await _claim_rotation_secret(session, profile)
    outcome = await finish_pg_password_rotation(session, profile)
    return AccessChange(previous, target, outcome)


def has_pending_rotation(profile: ResellerProfile | None) -> bool:
    return bool(profile is not None and (profile.pg_password_rotation_enc or "").strip())
