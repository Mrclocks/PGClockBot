"""Grant / manage web-panel access for existing PasarGuard admins.

A PasarGuard admin may already reach this web panel via several channels.
Only one login path is allowed per PG admin username:

1. Owner credentials (web_admin.json) — reserved usernames only
2. ResellerProfile linked by ``pg_admin_username`` (shop + PG ACL)
3. PgStaffAccess row (PG ACL only)

Creating a new staff grant must fail when any of these already apply.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PgStaffAccess, ResellerProfile
from app.services.web_auth import (
    hash_password,
    load_web_admin,
    validate_password_strength,
    validate_web_username,
    verify_password_hash,
)

# Shown on login / session kick when PG admin is disabled or removed.
PG_ACCESS_DENIED_MSG = (
    "دسترسی وب‌پنل برای شما فعال نیست. با ادمین اصلی تماس بگیرید."
)

PgAdminGate = Literal["ok", "disabled", "missing", "unreachable"]


def classify_pg_admin_dict(admin: dict | None) -> Literal["ok", "disabled", "missing"]:
    """Map a PasarGuard admin payload to a gate state.

    - ``missing``: admin fully deleted from PasarGuard
    - ``disabled``: deactivated / inactive — block login, keep web credentials
    - ``ok``: active, or merely limited / quota-exhausted / expired (keep account)
    """
    if not isinstance(admin, dict) or not admin:
        return "missing"
    if admin.get("enabled") is False or admin.get("is_disabled") is True:
        return "disabled"
    if admin.get("is_active") is False:
        return "disabled"
    status = str(admin.get("status") or "").strip().lower()
    if status in {"disabled", "inactive", "deleted", "banned"}:
        return "disabled"
    # limited / expired / on_hold / quota exhausted → keep web account & allow login
    return "ok"


async def fetch_pg_admin_gate(pg_username: str) -> tuple[PgAdminGate, dict | None]:
    """Live-check PasarGuard for this admin. Network errors → ``unreachable``."""
    uname = _norm_pg(pg_username)
    if not uname:
        return "missing", None
    from app.services.pasarguard import get_pg

    try:
        admin = await get_pg().get_admin(uname)
    except Exception:
        return "unreachable", None
    return classify_pg_admin_dict(admin), admin


# Short TTL cache so every authenticated HTML request does not hit PasarGuard.
_PG_GATE_CACHE: dict[str, tuple[float, tuple[bool, str | None]]] = {}
_PG_GATE_TTL_SEC = 45.0


def invalidate_pg_gate_cache(pg_username: str | None = None) -> None:
    """Evict one (or all) PG web-gate cache entries after revoke/disable."""
    if not pg_username:
        _PG_GATE_CACHE.clear()
        return
    uname = _norm_pg(pg_username)
    if uname:
        _PG_GATE_CACHE.pop(uname, None)


async def enforce_pg_admin_web_gate(
    session: AsyncSession,
    pg_username: str,
    *,
    revoke_if_missing: bool = True,
) -> tuple[bool, str | None]:
    """Return ``(allowed, error_message)``.

    When the PG admin was fully deleted, revoke ``PgStaffAccess`` (not reseller
    shop profiles). Quota/limit exhaustion never deletes the web row.
    """
    uname = _norm_pg(pg_username)
    now = time.monotonic()
    cached = _PG_GATE_CACHE.get(uname) if uname else None
    if cached and (now - cached[0]) < _PG_GATE_TTL_SEC:
        return cached[1]

    gate, _admin = await fetch_pg_admin_gate(pg_username)
    if gate == "ok":
        result: tuple[bool, str | None] = (True, None)
    elif gate == "unreachable":
        # Don't lock everyone out if PasarGuard is temporarily down
        result = (True, None)
    else:
        if gate == "missing" and revoke_if_missing:
            try:
                await revoke_web_access(session, pg_username)
            except Exception:
                pass
        result = (False, PG_ACCESS_DENIED_MSG)

    if uname:
        _PG_GATE_CACHE[uname] = (now, result)
        if len(_PG_GATE_CACHE) > 512:
            oldest = sorted(_PG_GATE_CACHE.items(), key=lambda kv: kv[1][0])[:128]
            for k, _ in oldest:
                _PG_GATE_CACHE.pop(k, None)
    return result


async def purge_orphaned_staff_access(session: AsyncSession) -> int:
    """Delete PgStaffAccess rows whose PG admin no longer exists in PasarGuard."""
    rows = await list_access_rows(session)
    if not rows:
        return 0
    from app.services.pasarguard import get_pg

    try:
        admins = await get_pg().get_admins()
    except Exception:
        return 0
    alive = {
        str(a.get("username") or "").strip().lower()
        for a in admins
        if isinstance(a, dict) and a.get("username")
    }
    removed = 0
    for row in rows:
        key = _norm_pg(row.pg_username)
        if key and key not in alive:
            await session.delete(row)
            removed += 1
    if removed:
        await session.commit()
    return removed


@dataclass(frozen=True)
class ExistingWebAccess:
    """How a PasarGuard admin already reaches the web panel (if at all)."""

    source: str  # none | pg_staff | reseller | owner
    pg_username: str
    web_username: str | None = None
    is_active: bool | None = None
    setup_complete: bool | None = None
    reseller_user_id: int | None = None
    reseller_profile_id: int | None = None
    staff_id: int | None = None
    note: str | None = None
    detail: str | None = None

    @property
    def has_access(self) -> bool:
        return self.source != "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "pg_username": self.pg_username,
            "web_username": self.web_username,
            "is_active": self.is_active,
            "setup_complete": self.setup_complete,
            "reseller_user_id": self.reseller_user_id,
            "reseller_profile_id": self.reseller_profile_id,
            "staff_id": self.staff_id,
            "note": self.note,
            "detail": self.detail,
            "has_access": self.has_access,
        }


def _norm_pg(username: str | None) -> str:
    return (username or "").strip().lower()


async def list_access_rows(session: AsyncSession) -> list[PgStaffAccess]:
    result = await session.execute(select(PgStaffAccess).order_by(PgStaffAccess.id.desc()))
    return list(result.scalars().all())


async def access_by_pg_username(session: AsyncSession, pg_username: str) -> PgStaffAccess | None:
    uname = _norm_pg(pg_username)
    if not uname:
        return None
    result = await session.execute(
        select(PgStaffAccess).where(PgStaffAccess.pg_username == uname)
    )
    return result.scalar_one_or_none()


async def access_by_web_username(session: AsyncSession, web_username: str) -> PgStaffAccess | None:
    uname = (web_username or "").strip().lower()
    if not uname:
        return None
    result = await session.execute(
        select(PgStaffAccess).where(PgStaffAccess.web_username == uname)
    )
    return result.scalar_one_or_none()


async def access_map_by_pg(session: AsyncSession) -> dict[str, PgStaffAccess]:
    rows = await list_access_rows(session)
    return {(r.pg_username or "").lower(): r for r in rows if r.pg_username}


async def reseller_by_pg_username(
    session: AsyncSession, pg_username: str
) -> ResellerProfile | None:
    """Find a reseller profile linked to this PasarGuard admin (case-insensitive)."""
    uname = _norm_pg(pg_username)
    if not uname:
        return None
    result = await session.execute(
        select(ResellerProfile).where(
            ResellerProfile.pg_admin_username.is_not(None),
            func.lower(ResellerProfile.pg_admin_username) == uname,
        )
    )
    return result.scalars().first()


async def resolve_existing_web_access(
    session: AsyncSession, pg_username: str
) -> ExistingWebAccess:
    """Detect every way this PG admin may already log into the web panel."""
    pg_u = _norm_pg(pg_username)
    if not pg_u:
        return ExistingWebAccess(source="none", pg_username="")

    owner_u = (load_web_admin().get("username") or "").strip().lower()
    if owner_u and pg_u == owner_u:
        return ExistingWebAccess(
            source="owner",
            pg_username=pg_u,
            web_username=owner_u,
            is_active=True,
            detail="این نام با ادمین اصلی وب‌پنل یکی است و از قبل دسترسی کامل دارد",
        )

    staff = await access_by_pg_username(session, pg_u)
    reseller = await reseller_by_pg_username(session, pg_u)

    # Prefer reseller as the authoritative shop path when both somehow exist.
    # Any reseller link (even inactive / no web creds yet) reserves this PG admin —
    # matches web_access_status_map and the one-path invariant in the module doc.
    if reseller is not None:
        has_web = bool(reseller.web_username and reseller.web_password_hash)
        from app.services.resellers import setup_is_complete

        complete = setup_is_complete(reseller)
        detail = (
            f"این ادمین به نماینده متصل است"
            f"{f' (یوزر وب: {reseller.web_username})' if reseller.web_username else ''}"
            " — از بخش نمایندگان مدیریت شود"
        )
        if staff is not None:
            detail += "؛ دسترسی جداگانه pg_staff هم ثبت شده و باید یکی حذف شود"
        return ExistingWebAccess(
            source="reseller",
            pg_username=pg_u,
            web_username=reseller.web_username,
            is_active=bool(reseller.is_active) and has_web,
            setup_complete=complete,
            reseller_user_id=int(reseller.user_id),
            reseller_profile_id=int(reseller.id),
            staff_id=int(staff.id) if staff else None,
            note=reseller.web_username,
            detail=detail,
        )

    if staff is not None:
        return ExistingWebAccess(
            source="pg_staff",
            pg_username=pg_u,
            web_username=staff.web_username,
            is_active=bool(staff.is_active),
            staff_id=int(staff.id),
            note=staff.note,
            detail="دسترسی وب‌پنل از قبل برای این ادمین ساخته شده",
        )

    return ExistingWebAccess(source="none", pg_username=pg_u)


async def web_access_status_map(
    session: AsyncSession, pg_usernames: list[str]
) -> dict[str, dict[str, Any]]:
    """Build UI status map keyed by lowercased PG username."""
    out: dict[str, dict[str, Any]] = {}
    # Prefetch staff + reseller rows once for efficiency
    staff_map = await access_map_by_pg(session)
    reseller_rows = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.pg_admin_username.is_not(None))
        )
    ).scalars().all()
    reseller_map: dict[str, ResellerProfile] = {}
    for r in reseller_rows:
        key = _norm_pg(r.pg_admin_username)
        if key and key not in reseller_map:
            reseller_map[key] = r

    owner_u = (load_web_admin().get("username") or "").strip().lower()
    from app.services.resellers import setup_is_complete

    seen: set[str] = set()
    for raw in pg_usernames:
        key = _norm_pg(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        if owner_u and key == owner_u:
            out[key] = ExistingWebAccess(
                source="owner",
                pg_username=key,
                web_username=owner_u,
                is_active=True,
                detail="ادمین اصلی وب‌پنل",
            ).as_dict()
            continue
        reseller = reseller_map.get(key)
        staff = staff_map.get(key)
        if reseller is not None:
            has_web = bool(reseller.web_username and reseller.web_password_hash)
            out[key] = ExistingWebAccess(
                source="reseller",
                pg_username=key,
                web_username=reseller.web_username,
                is_active=bool(reseller.is_active) and has_web,
                setup_complete=setup_is_complete(reseller),
                reseller_user_id=int(reseller.user_id),
                reseller_profile_id=int(reseller.id),
                staff_id=int(staff.id) if staff else None,
                detail=(
                    f"نماینده"
                    f"{f' · {reseller.web_username}' if reseller.web_username else ' · بدون یوزر وب هنوز'}"
                ),
            ).as_dict()
            continue
        if staff is not None:
            out[key] = ExistingWebAccess(
                source="pg_staff",
                pg_username=key,
                web_username=staff.web_username,
                is_active=bool(staff.is_active),
                staff_id=int(staff.id),
                note=staff.note,
                detail="دسترسی وب ادمین پاسارگارد",
            ).as_dict()
            continue
        out[key] = ExistingWebAccess(source="none", pg_username=key).as_dict()
    return out


async def conflict_message_for_new_grant(
    session: AsyncSession, pg_username: str
) -> str | None:
    """Return a Persian error if a new PgStaffAccess must not be created."""
    existing = await resolve_existing_web_access(session, pg_username)
    if existing.source == "none":
        return None
    if existing.source == "owner":
        return existing.detail or "این نام برای ادمین اصلی وب‌پنل رزرو است"
    if existing.source == "reseller":
        parts = [
            "این ادمین پاسارگارد از قبل به یک نماینده متصل است و دسترسی وب‌پنل دارد یا خواهد داشت."
        ]
        if existing.web_username:
            parts.append(f"یوزر وب فعلی نماینده: «{existing.web_username}».")
        else:
            parts.append("نماینده هنوز یوزر وب نساخته؛ از بخش نمایندگان / لینک راه‌اندازی پیگیری کنید.")
        if existing.reseller_user_id:
            parts.append("ساخت دسترسی جداگانه مجاز نیست.")
        return " ".join(parts)
    if existing.source == "pg_staff":
        return (
            f"برای این ادمین از قبل دسترسی وب‌پنل ساخته شده"
            f"{f' (یوزر: «{existing.web_username}»)' if existing.web_username else ''}."
            " برای تغییر، همان ردیف را ویرایش کنید — ساخت جدید مجاز نیست."
        )
    return existing.detail or "این ادمین از قبل دسترسی وب‌پنل دارد"


async def conflict_message_for_reseller_link(
    session: AsyncSession,
    pg_username: str,
    *,
    exclude_profile_id: int | None = None,
) -> str | None:
    """Block linking a reseller to a PG admin that already has staff web access / another reseller."""
    pg_u = _norm_pg(pg_username)
    if not pg_u:
        return None
    other = await reseller_by_pg_username(session, pg_u)
    if other is not None and (
        exclude_profile_id is None or int(other.id) != int(exclude_profile_id)
    ):
        return (
            f"ادمین پاسارگارد «{pg_u}» قبلاً به نماینده دیگری متصل است"
            f"{f' (یوزر وب: {other.web_username})' if other.web_username else ''}."
        )
    staff = await access_by_pg_username(session, pg_u)
    if staff is not None:
        return (
            f"ادمین پاسارگارد «{pg_u}» قبلاً دسترسی وب‌پنل جداگانه دارد"
            f" (یوزر: «{staff.web_username}»). "
            "ابتدا آن دسترسی را از صفحه ادمین پاسارگارد حذف کنید، بعد نماینده را وصل کنید."
        )
    return None


async def _username_taken(
    session: AsyncSession,
    web_username: str,
    *,
    exclude_staff_id: int | None = None,
    exclude_reseller_id: int | None = None,
) -> str | None:
    cleaned, err = validate_web_username(web_username, lowercase=True)
    if err:
        return err
    admin_u = (load_web_admin().get("username") or "").strip().lower()
    if cleaned == admin_u:
        return "این نام کاربری برای ادمین اصلی پنل رزرو است"
    q_res = select(ResellerProfile).where(ResellerProfile.web_username == cleaned)
    if exclude_reseller_id is not None:
        q_res = q_res.where(ResellerProfile.id != int(exclude_reseller_id))
    if (await session.execute(q_res)).scalar_one_or_none():
        return "این نام کاربری قبلاً برای یک نماینده گرفته شده"
    q = select(PgStaffAccess).where(PgStaffAccess.web_username == cleaned)
    if exclude_staff_id is not None:
        q = q.where(PgStaffAccess.id != int(exclude_staff_id))
    if (await session.execute(q)).scalar_one_or_none():
        return "این نام کاربری قبلاً برای دسترسی وب ادمین پاسارگارد گرفته شده"
    return None


async def _sync_pg_admin_password(
    pg_username: str, password: str
) -> tuple[str | None, str | None]:
    """Set PasarGuard admin password to match web credentials.

    Returns ``(pg_password_enc, error_message)``.
    Unified web/PG password (same model as reseller provision) so
    ``get_pg_for_staff`` authenticates as the limited admin — never Owner.
    """
    from app.services.pasarguard import get_pg, invalidate_staff_pg_client
    from app.services.secret_box import encrypt_secret

    pwd = (password or "").strip()
    if not pwd:
        return None, "رمز عبور الزامی است"
    try:
        await get_pg().modify_admin(pg_username, {"password": pwd})
    except Exception as exc:
        return None, f"همگام‌سازی رمز پاسارگارد ناموفق بود: {exc}"
    invalidate_staff_pg_client(pg_username)
    return encrypt_secret(pwd), None


async def grant_web_access(
    session: AsyncSession,
    *,
    pg_username: str,
    web_username: str,
    password: str,
    note: str = "",
    is_active: bool = True,
) -> tuple[PgStaffAccess | None, str | None]:
    """Create a new PgStaffAccess only when no other web path exists."""
    pg_u = _norm_pg(pg_username)
    if not pg_u:
        return None, "نام ادمین پاسارگارد الزامی است"
    if not (password or "").strip():
        return None, "رمز عبور الزامی است"
    ok, err = validate_password_strength(password)
    if not ok:
        return None, err

    cleaned, uerr = validate_web_username(web_username, lowercase=True)
    if uerr:
        return None, uerr

    conflict = await conflict_message_for_new_grant(session, pg_u)
    if conflict:
        return None, conflict

    taken = await _username_taken(session, cleaned)
    if taken:
        return None, taken

    enc, sync_err = await _sync_pg_admin_password(pg_u, password)
    if sync_err or not enc:
        return None, sync_err or "همگام‌سازی رمز پاسارگارد ناموفق بود"

    row = PgStaffAccess(
        pg_username=pg_u,
        web_username=cleaned,
        web_password_hash=hash_password(password),
        pg_password_enc=enc,
        is_active=bool(is_active),
        note=(note or "").strip() or None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, None


async def update_web_access(
    session: AsyncSession,
    *,
    pg_username: str,
    web_username: str,
    password: str,
    note: str = "",
    is_active: bool | None = None,
) -> tuple[PgStaffAccess | None, str | None]:
    """Update credentials for an existing PgStaffAccess row only."""
    pg_u = _norm_pg(pg_username)
    existing = await access_by_pg_username(session, pg_u)
    if not existing:
        return None, "دسترسی وب برای این ادمین وجود ندارد — ابتدا اعطا کنید"

    # Still refuse if a reseller link appeared (data race / manual DB edit)
    reseller = await reseller_by_pg_username(session, pg_u)
    if reseller is not None:
        return None, (
            "این ادمین الان به نماینده متصل است؛ ویرایش دسترسی جداگانه مجاز نیست. "
            "از بخش نمایندگان مدیریت کنید یا اتصال نماینده را بردارید."
        )

    if not (password or "").strip():
        # Keep existing password on edit
        pass
    else:
        ok, err = validate_password_strength(password)
        if not ok:
            return None, err
    cleaned, uerr = validate_web_username(web_username, lowercase=True)
    if uerr:
        return None, uerr
    taken = await _username_taken(session, cleaned, exclude_staff_id=existing.id)
    if taken:
        return None, taken

    existing.web_username = cleaned
    if (password or "").strip():
        enc, sync_err = await _sync_pg_admin_password(pg_u, password)
        if sync_err or not enc:
            return None, sync_err or "همگام‌سازی رمز پاسارگارد ناموفق بود"
        existing.web_password_hash = hash_password(password)
        existing.pg_password_enc = enc
    if is_active is not None:
        existing.is_active = bool(is_active)
    if note is not None:
        existing.note = (note or "").strip() or None
    await session.commit()
    await session.refresh(existing)
    return existing, None


async def upsert_web_access(
    session: AsyncSession,
    *,
    pg_username: str,
    web_username: str,
    password: str,
    note: str = "",
    is_active: bool = True,
) -> tuple[PgStaffAccess | None, str | None]:
    """Backward-compatible entry: update if staff row exists, else grant (with guards)."""
    existing = await access_by_pg_username(session, pg_username)
    if existing:
        return await update_web_access(
            session,
            pg_username=pg_username,
            web_username=web_username,
            password=password,
            note=note,
            is_active=is_active,
        )
    return await grant_web_access(
        session,
        pg_username=pg_username,
        web_username=web_username,
        password=password,
        note=note,
        is_active=is_active,
    )


async def revoke_web_access(session: AsyncSession, pg_username: str) -> bool:
    row = await access_by_pg_username(session, pg_username)
    if not row:
        return False
    await session.delete(row)
    await session.commit()
    invalidate_pg_gate_cache(pg_username)
    from app.services.pasarguard import invalidate_staff_pg_client

    invalidate_staff_pg_client(pg_username)
    return True


async def set_active(session: AsyncSession, pg_username: str, active: bool) -> bool:
    row = await access_by_pg_username(session, pg_username)
    if not row:
        return False
    # Do not toggle staff access while a reseller owns this PG admin
    reseller = await reseller_by_pg_username(session, pg_username)
    if reseller is not None:
        return False
    row.is_active = bool(active)
    await session.commit()
    # Immediate eviction so revoked/disabled staff cannot keep using a cached allow.
    invalidate_pg_gate_cache(pg_username)
    return True


async def change_staff_credentials(
    session: AsyncSession,
    row: PgStaffAccess,
    *,
    old_username: str,
    current_password: str,
    new_username: str,
    new_password: str,
) -> tuple[PgStaffAccess | None, str | None]:
    if (old_username or "").strip().lower() != (row.web_username or "").lower():
        return None, "یوزر یا رمز قدیم اشتباه است"
    if not verify_password_hash(current_password or "", row.web_password_hash):
        return None, "یوزر یا رمز قدیم اشتباه است"
    ok, err = validate_password_strength(new_password or "")
    if not ok:
        return None, err
    cleaned, uerr = validate_web_username(new_username, lowercase=True)
    if uerr:
        return None, uerr
    taken = await _username_taken(session, cleaned, exclude_staff_id=row.id)
    if taken:
        return None, taken
    enc, sync_err = await _sync_pg_admin_password(row.pg_username, new_password)
    if sync_err or not enc:
        return None, sync_err or "همگام‌سازی رمز پاسارگارد ناموفق بود"
    row.web_username = cleaned
    row.web_password_hash = hash_password(new_password)
    row.pg_password_enc = enc
    await session.commit()
    await session.refresh(row)
    return row, None


async def resolve_pg_role_id_for_admin(pg_username: str) -> int | None:
    """Look up role_id for a PasarGuard admin username."""
    from app.services.pasarguard import get_pg

    try:
        admin = await get_pg().get_admin(pg_username)
    except Exception:
        return None
    if not isinstance(admin, dict):
        return None
    role = admin.get("role")
    if isinstance(role, dict) and role.get("id") is not None:
        try:
            return int(role["id"])
        except (TypeError, ValueError):
            pass
    for key in ("role_id", "admin_role_id"):
        if admin.get(key) is not None:
            try:
                return int(admin[key])
            except (TypeError, ValueError):
                pass
    return None
