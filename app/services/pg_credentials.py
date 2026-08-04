"""PasarGuard credential readiness + admin-initiated repair.

Non-owner staff must authenticate as their own PG admin. Missing encrypted
passwords (legacy rows) fail closed — never fall back to the Owner token.
Platform admin can repair by resetting the PG admin password and storing it.
"""

from __future__ import annotations

import secrets
import string
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PgStaffAccess, ResellerProfile
from app.services.secret_box import decrypt_secret, encrypt_secret


PG_CREDENTIAL_MISSING_MSG = (
    "رمز پاسارگارد برای این حساب ذخیره نشده است. "
    "از ادمین اصلی بخواهید رمز را همگام‌سازی کند یا از بخش امنیت رمز را عوض کنید."
)


def _rand_pg_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    # Ensure mixed class for validate_password_strength-like rules
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    chars += [secrets.choice(alphabet) for _ in range(max(0, length - 4))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def enc_has_secret(enc: str | None) -> bool:
    """True when ciphertext decrypts to a non-empty password."""
    return bool(decrypt_secret(enc))


def staff_pg_client_ready(staff: dict | None) -> bool:
    """Platform admin is always ready; others need the flag set by require_staff."""
    if not staff:
        return False
    if staff.get("role") == "admin":
        return True
    return bool(staff.get("pg_client_ready"))


async def reseller_has_pg_credentials(
    session: AsyncSession, reseller_user_id: int
) -> bool:
    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_user_id))
        )
    ).scalar_one_or_none()
    if not profile or not (profile.pg_admin_username or "").strip():
        return False
    return enc_has_secret(profile.pg_admin_password_enc)


async def pg_staff_has_credentials(session: AsyncSession, pg_username: str) -> bool:
    from app.services.pg_staff_access import access_by_pg_username

    row = await access_by_pg_username(session, pg_username)
    if not row or not row.is_active:
        return False
    return enc_has_secret(row.pg_password_enc)


async def list_resellers_missing_pg_credentials(
    session: AsyncSession,
) -> list[ResellerProfile]:
    """Resellers linked to a PG admin/role but without a usable stored password."""
    rows = (
        await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.pg_admin_username.is_not(None),
                ResellerProfile.is_active.is_(True),
            )
        )
    ).scalars().all()
    out: list[ResellerProfile] = []
    for r in rows:
        if not (r.pg_admin_username or "").strip():
            continue
        # Role id optional — username link is enough to need a client password.
        if not enc_has_secret(r.pg_admin_password_enc):
            out.append(r)
    return out


async def list_pg_staff_missing_credentials(
    session: AsyncSession,
) -> list[PgStaffAccess]:
    rows = (
        await session.execute(
            select(PgStaffAccess).where(PgStaffAccess.is_active.is_(True))
        )
    ).scalars().all()
    return [r for r in rows if not enc_has_secret(r.pg_password_enc)]


async def repair_reseller_pg_credentials(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    password: str | None = None,
    sync_web_password: bool = False,
) -> tuple[str | None, str | None]:
    """Reset PG admin password via Owner API and store ciphertext.

    Returns ``(plaintext_password, error)``. Never returns Owner token usage
    for subsequent staff ops — only stores the new limited-admin secret.
    """
    from app.services.pasarguard import get_pg, invalidate_reseller_pg_client
    from app.services.web_auth import hash_password, validate_password_strength

    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        return None, "ادمین پاسارگارد برای این نماینده تعریف نشده"
    pwd = (password or "").strip() or _rand_pg_password()
    ok, err = validate_password_strength(pwd)
    if not ok:
        return None, err
    try:
        await get_pg().modify_admin(uname, {"password": pwd})
    except Exception as exc:
        return None, f"همگام‌سازی رمز پاسارگارد ناموفق بود: {exc}"
    profile.pg_admin_password_enc = encrypt_secret(pwd)
    if sync_web_password:
        profile.web_password_hash = hash_password(pwd)
    await session.commit()
    invalidate_reseller_pg_client(int(profile.user_id))
    return pwd, None


async def repair_pg_staff_credentials(
    session: AsyncSession,
    row: PgStaffAccess,
    *,
    password: str | None = None,
    sync_web_password: bool = False,
) -> tuple[str | None, str | None]:
    """Reset PG admin password for a pg_staff row and store ciphertext."""
    from app.services.pasarguard import get_pg, invalidate_staff_pg_client
    from app.services.web_auth import hash_password, validate_password_strength

    uname = (row.pg_username or "").strip()
    if not uname:
        return None, "نام ادمین پاسارگارد نامعتبر است"
    pwd = (password or "").strip() or _rand_pg_password()
    ok, err = validate_password_strength(pwd)
    if not ok:
        return None, err
    try:
        await get_pg().modify_admin(uname, {"password": pwd})
    except Exception as exc:
        return None, f"همگام‌سازی رمز پاسارگارد ناموفق بود: {exc}"
    row.pg_password_enc = encrypt_secret(pwd)
    if sync_web_password:
        row.web_password_hash = hash_password(pwd)
    await session.commit()
    invalidate_staff_pg_client(uname)
    return pwd, None


def credential_status_for_reseller(profile: ResellerProfile | None) -> dict[str, Any]:
    if not profile:
        return {"linked": False, "ready": False}
    linked = bool((profile.pg_admin_username or "").strip())
    ready = linked and enc_has_secret(profile.pg_admin_password_enc)
    return {
        "linked": linked,
        "ready": ready,
        "pg_username": (profile.pg_admin_username or "").strip() or None,
        "pg_role_id": int(profile.pg_role_id) if profile.pg_role_id else None,
        "needs_repair": linked and not ready,
    }


def credential_status_for_staff(row: PgStaffAccess | None) -> dict[str, Any]:
    if not row:
        return {"linked": False, "ready": False}
    linked = bool((row.pg_username or "").strip())
    ready = linked and enc_has_secret(row.pg_password_enc)
    return {
        "linked": linked,
        "ready": ready,
        "pg_username": (row.pg_username or "").strip() or None,
        "needs_repair": linked and not ready,
    }
