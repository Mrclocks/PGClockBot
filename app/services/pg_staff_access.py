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
# Phase 1F M3 — PG identity/authorization cannot be resolved safely.
PG_UNAVAILABLE_MSG = (
    "ارتباط با پاسارگارد برقرار نیست — تا برقراری ارتباط، ورود/دسترسی مسدود است."
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
    """Live-check PasarGuard for this admin. Network errors → ``unreachable``.

    Uses ``get_admin_gate`` (not the plain ``get_admin``) because the latter
    swallows every lookup exception internally and returns ``None`` both when
    the admin was genuinely deleted (404) and when PasarGuard was merely
    unreachable (timeout/5xx) — which used to make a temporary PasarGuard
    outage permanently revoke a legitimate pg_staff web account.
    """
    uname = _norm_pg(pg_username)
    if not uname:
        return "missing", None
    from app.services.pasarguard import get_pg

    try:
        network_gate, admin = await get_pg().get_admin_gate(uname)
    except Exception:
        return "unreachable", None
    if network_gate == "unreachable":
        return "unreachable", None
    if network_gate == "missing":
        return "missing", None
    return classify_pg_admin_dict(admin), admin


# Short TTL cache so every authenticated HTML request does not hit PasarGuard.
_PG_GATE_CACHE: dict[str, tuple[float, tuple[bool, str | None]]] = {}
_PG_GATE_TTL_SEC = 45.0


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
        # M3: cannot safely resolve PG identity → deny access (do NOT revoke).
        # Never convert outage into allow / unrestricted menus.
        result = (False, PG_UNAVAILABLE_MSG)
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


async def _detach_pg_staff_fk_deps(session: AsyncSession, staff_id: int) -> None:
    """Clear org_principals / panel_tickets FKs before deleting PgStaffAccess."""
    from sqlalchemy import update

    from app.db.models import PanelTicket
    from app.services.org_principals import purge_principal_for_pg_staff

    sid = int(staff_id)
    if sid <= 0:
        return
    await purge_principal_for_pg_staff(session, sid)
    await session.execute(
        update(PanelTicket)
        .where(PanelTicket.opener_pg_staff_id == sid)
        .values(opener_pg_staff_id=None)
    )


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
            await _detach_pg_staff_fk_deps(session, int(row.id))
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
    # Phase D3 — pg_staff remediation visibility (None for non-staff sources)
    credentials_ready: bool | None = None
    username_aligned: bool | None = None
    needs_remediation: bool | None = None

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
            "credentials_ready": self.credentials_ready,
            "username_aligned": self.username_aligned,
            "needs_remediation": self.needs_remediation,
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
        flags = staff_remediation_flags(staff)
        return ExistingWebAccess(
            source="pg_staff",
            pg_username=pg_u,
            web_username=staff.web_username,
            is_active=bool(staff.is_active),
            staff_id=int(staff.id),
            note=staff.note,
            detail="دسترسی وب‌پنل از قبل برای این ادمین ساخته شده",
            **flags,
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
                    "نماینده"
                    f"{f' · {reseller.web_username}' if reseller.web_username else ' · بدون یوزر وب هنوز'}"
                    + (" · ردیف ادمین فرعی یتیم — حذف کنید" if staff else "")
                ),
                note=("orphan_pg_staff" if staff else None),
            ).as_dict()
            continue
        if staff is not None:
            flags = staff_remediation_flags(staff)
            out[key] = ExistingWebAccess(
                source="pg_staff",
                pg_username=key,
                web_username=staff.web_username,
                is_active=bool(staff.is_active),
                staff_id=int(staff.id),
                note=staff.note,
                detail="دسترسی وب ادمین پاسارگارد",
                **flags,
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


def assert_web_matches_pg(web_username: str, pg_username: str) -> str | None:
    """Phase D2: pg_staff web login must equal PG admin username (error-only, no rename)."""
    web = (web_username or "").strip().lower()
    pg = _norm_pg(pg_username)
    if not web or not pg:
        return "نام کاربری وب و پاسارگارد الزامی است"
    if web != pg:
        return (
            "برای ادمین فرعی، نام کاربری وب باید دقیقاً همان نام ادمین پاسارگارد باشد "
            f"(«{pg}») — تغییر خودکار انجام نمی‌شود"
        )
    return None


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

    cleaned, uerr = validate_web_username(web_username, lowercase=True)
    if uerr:
        return None, uerr
    mismatch = assert_web_matches_pg(cleaned, pg_u)
    if mismatch:
        return None, mismatch
    ok, err = validate_password_strength(password, username=cleaned or pg_u)
    if not ok:
        return None, err

    conflict = await conflict_message_for_new_grant(session, pg_u)
    if conflict:
        return None, conflict

    taken = await _username_taken(session, cleaned)
    if taken:
        return None, taken

    from app.services.pasarguard import get_pg, reset_pg
    from app.services.secret_box import encrypt_secret

    # Encrypt first so a Fernet failure never leaves PasarGuard with a new
    # password while the local row/enc is missing (partial-failure trap).
    enc = encrypt_secret(password)
    if not enc:
        return None, (
            "رمز‌گذاری رمز پاسارگارد ناموفق بود. "
            "علت محتمل: کلید رمزنگاری پنل تنظیم نشده یا خراب است. "
            "راه حل: تنظیمات امنیتی سرور را بررسی کنید و دوباره تلاش کنید — "
            "رمز پاسارگارد هنوز تغییر نکرده است."
        )
    try:
        await get_pg().modify_admin(pg_u, {"password": password})
        reset_pg()
    except Exception as e:
        return None, (
            f"همگام‌سازی رمز با پاسارگارد ناموفق بود: {e}. "
            "علت محتمل: ارتباط با پاسارگارد یا رد شدن رمز توسط قوانین پنل. "
            "راه حل: اتصال و قوانین رمز پاسارگارد را بررسی کنید و دوباره تلاش کنید."
        )

    role_id = await resolve_pg_role_id_for_admin(pg_u)
    row = PgStaffAccess(
        pg_username=pg_u,
        web_username=cleaned,
        web_password_hash=hash_password(password),
        pg_admin_password_enc=enc,
        pg_role_id=int(role_id) if role_id else None,
        is_active=bool(is_active),
        note=(note or "").strip() or None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    # Optional time subscription when caller passes plan_id via note marker — see grant with plan
    return row, None


async def grant_web_access_with_plan(
    session: AsyncSession,
    *,
    pg_username: str,
    web_username: str,
    password: str,
    plan_id: int | None = None,
    note: str = "",
    is_active: bool = True,
) -> tuple[PgStaffAccess | None, str | None]:
    """Grant pg_staff and optionally bind a subscription plan (time + base capacity)."""
    row, err = await grant_web_access(
        session,
        pg_username=pg_username,
        web_username=web_username,
        password=password,
        note=note,
        is_active=is_active,
    )
    if err or row is None:
        return row, err
    if plan_id:
        from app.db.models import ResellerPlan
        from app.services.pg_admin_subscription import (
            is_subscription_plan,
            start_or_refresh_subscription,
        )

        plan = await session.get(ResellerPlan, int(plan_id))
        if plan and is_subscription_plan(plan):
            try:
                await start_or_refresh_subscription(
                    session,
                    pg_username=row.pg_username,
                    plan=plan,
                    reset_extras=True,
                    apply_pg_limits=True,
                )
                await session.commit()
            except Exception as e:
                return row, f"دسترسی وب ساخته شد ولی اشتراک زمانی اعمال نشد: {e}"
    return row, None


async def update_web_access(
    session: AsyncSession,
    *,
    pg_username: str,
    web_username: str,
    password: str,
    note: str = "",
    is_active: bool | None = None,
    confirm_align: bool = False,
) -> tuple[PgStaffAccess | None, str | None]:
    """Update credentials for an existing PgStaffAccess row only.

    Phase D3 Q1 A: when DB ``web_username ≠ pg_username``, setting web to the
    PG name is an explicit align (requires ``confirm_align``) — never silent.
    """
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

    pwd = (password or "").strip()
    if not pwd:
        # Keep existing password on edit — but not when PG enc is missing
        if not staff_has_stored_pg_password(existing):
            return None, "رمز عبور الزامی است — اعتبارنامه پاسارگارد ذخیره نشده"
    else:
        cleaned_tmp, _ = validate_web_username(web_username, lowercase=True)
        ok, err = validate_password_strength(
            password, username=(cleaned_tmp or pg_u)
        )
        if not ok:
            return None, err
    cleaned, uerr = validate_web_username(web_username, lowercase=True)
    if uerr:
        return None, uerr

    existing_web = (existing.web_username or "").strip().lower()
    # D3 Q1 A: aligning a mismatched username requires explicit Owner confirmation
    if existing_web and existing_web != pg_u and cleaned == pg_u and not confirm_align:
        return None, (
            f"نام کاربری وب («{existing_web}») با پاسارگارد («{pg_u}») یکی نیست. "
            "برای هم‌ترازسازی، گزینه تأیید را علامت بزنید — تغییر خودکار انجام نمی‌شود"
        )

    mismatch = assert_web_matches_pg(cleaned, pg_u)
    if mismatch:
        return None, mismatch
    taken = await _username_taken(session, cleaned, exclude_staff_id=existing.id)
    if taken:
        return None, taken

    existing.web_username = cleaned
    if pwd:
        from app.services.pasarguard import get_pg, reset_pg
        from app.services.secret_box import encrypt_secret

        enc = encrypt_secret(pwd)
        if not enc:
            return None, (
                "رمز‌گذاری رمز پاسارگارد ناموفق بود. "
                "علت محتمل: کلید رمزنگاری پنل تنظیم نشده یا خراب است. "
                "راه حل: تنظیمات امنیتی سرور را بررسی کنید و دوباره تلاش کنید — "
                "رمز پاسارگارد هنوز تغییر نکرده است."
            )
        try:
            await get_pg().modify_admin(pg_u, {"password": pwd})
            reset_pg()
        except Exception as e:
            return None, (
                f"همگام‌سازی رمز با پاسارگارد ناموفق بود: {e}. "
                "علت محتمل: ارتباط با پاسارگارد یا رد شدن رمز توسط قوانین پنل. "
                "راه حل: اتصال و قوانین رمز پاسارگارد را بررسی کنید و دوباره تلاش کنید."
            )
        existing.web_password_hash = hash_password(pwd)
        existing.pg_admin_password_enc = enc
    if is_active is not None:
        existing.is_active = bool(is_active)
    if note is not None:
        existing.note = (note or "").strip() or None
    # Refresh cached role id when possible
    role_id = await resolve_pg_role_id_for_admin(pg_u)
    if role_id:
        existing.pg_role_id = int(role_id)
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
    confirm_align: bool = False,
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
            confirm_align=confirm_align,
        )
    return await grant_web_access(
        session,
        pg_username=pg_username,
        web_username=web_username,
        password=password,
        note=note,
        is_active=is_active,
    )


def staff_has_stored_pg_password(row: PgStaffAccess | None) -> bool:
    """True when encrypted PasarGuard password decrypts to a non-empty secret."""
    if row is None:
        return False
    from app.services.secret_box import decrypt_secret

    return bool(decrypt_secret(getattr(row, "pg_admin_password_enc", None)))


def staff_username_aligned(row: PgStaffAccess | None) -> bool:
    """True when web login username equals PG admin username (case-insensitive)."""
    if row is None:
        return False
    web = (getattr(row, "web_username", None) or "").strip().lower()
    pg = _norm_pg(getattr(row, "pg_username", None))
    return bool(web and pg and web == pg)


def staff_needs_remediation(row: PgStaffAccess | None) -> bool:
    """True when enc is missing or username is misaligned (active or not)."""
    if row is None:
        return False
    return (not staff_has_stored_pg_password(row)) or (not staff_username_aligned(row))


def staff_remediation_flags(row: PgStaffAccess) -> dict[str, bool]:
    """Flags for Owner UI / status map (Phase D3)."""
    ready = staff_has_stored_pg_password(row)
    aligned = staff_username_aligned(row)
    return {
        "credentials_ready": ready,
        "username_aligned": aligned,
        "needs_remediation": (not ready) or (not aligned),
    }


def classify_staff_cohort(row: PgStaffAccess) -> str:
    """Legacy cohort label (L1–L5) for inventory / docs."""
    if not bool(row.is_active):
        return "L5"
    ready = staff_has_stored_pg_password(row)
    aligned = staff_username_aligned(row)
    if not ready and aligned:
        return "L1"
    if not ready and not aligned:
        return "L2"
    if ready and aligned:
        return "L3"
    return "L4"  # ready + misaligned


async def inventory_staff_remediation(
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Read-only inventory of pg_staff rows with cohort + readiness flags.

    Never returns passwords or encrypted blobs — ops visibility only.
    """
    rows = await list_access_rows(session)
    out: list[dict[str, Any]] = []
    for row in rows:
        flags = staff_remediation_flags(row)
        out.append(
            {
                "id": int(row.id),
                "pg_username": row.pg_username,
                "web_username": row.web_username,
                "is_active": bool(row.is_active),
                "cohort": classify_staff_cohort(row),
                "pg_role_id": int(row.pg_role_id) if row.pg_role_id else None,
                **flags,
            }
        )
    return out


async def revoke_web_access(
    session: AsyncSession, pg_username: str, *, commit: bool = True
) -> bool:
    row = await access_by_pg_username(session, pg_username)
    if not row:
        return False

    await _detach_pg_staff_fk_deps(session, int(row.id))
    await session.delete(row)
    if commit:
        await session.commit()
    else:
        await session.flush()
    try:
        from app.services.pasarguard import reset_pg

        reset_pg()
    except Exception:
        pass
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
        return None, (
            "یوزر یا رمز قدیم اشتباه است. "
            "علت محتمل: تایپ اشتباه یا رمز اخیراً توسط ادمین اصلی تغییر کرده. "
            "راه حل: مقادیر فعلی را دوباره وارد کنید یا از ادمین اصلی بخواهید رمز را بازنشانی کند."
        )
    ok, err = validate_password_strength(
        new_password or "", username=(new_username or row.pg_username)
    )
    if not ok:
        return None, err
    cleaned, uerr = validate_web_username(new_username, lowercase=True)
    if uerr:
        return None, uerr

    # D3: misaligned username — Owner confirm_align only (no self-serve rename)
    existing_web = (row.web_username or "").strip().lower()
    pg_u = _norm_pg(row.pg_username)
    if existing_web and pg_u and existing_web != pg_u:
        return None, (
            "نام کاربری وب با پاسارگارد یکی نیست. "
            "علت محتمل: حساب قدیمی قبل از یکپارچه‌سازی نام کاربری. "
            "راه حل: ادمین اصلی از بخش «ادمین‌ها» با تأیید هم‌ترازسازی، دسترسی ادمین فرعی را ویرایش کند."
        )

    mismatch = assert_web_matches_pg(cleaned, row.pg_username)
    if mismatch:
        return None, mismatch
    taken = await _username_taken(session, cleaned, exclude_staff_id=row.id)
    if taken:
        return None, taken
    from app.services.pasarguard import get_pg, reset_pg
    from app.services.secret_box import encrypt_secret

    enc = encrypt_secret(new_password)
    if not enc:
        return None, (
            "رمز‌گذاری رمز پاسارگارد ناموفق بود. "
            "علت محتمل: کلید رمزنگاری پنل تنظیم نشده یا خراب است. "
            "راه حل: تنظیمات امنیتی سرور را بررسی کنید و دوباره تلاش کنید — "
            "رمز پاسارگارد هنوز تغییر نکرده است."
        )
    try:
        await get_pg().modify_admin(row.pg_username, {"password": new_password})
        reset_pg()
    except Exception as e:
        return None, (
            f"همگام‌سازی رمز با پاسارگارد ناموفق بود: {e}. "
            "علت محتمل: ارتباط با پاسارگارد یا رد شدن رمز توسط قوانین پنل. "
            "راه حل: اتصال و قوانین رمز پاسارگارد را بررسی کنید و دوباره تلاش کنید."
        )
    row.web_username = cleaned
    row.web_password_hash = hash_password(new_password)
    row.pg_admin_password_enc = enc
    await session.commit()
    await session.refresh(row)
    return row, None


async def resolve_pg_role_id_for_admin(
    pg_username: str, *, client: Any | None = None
) -> int | None:
    """Look up role_id for a PasarGuard admin username.

    ``client`` must be the caller-allowed PG client. Omitting it uses Owner
    ``get_pg()`` — Owner/platform paths only.
    """
    pg = client
    if pg is None:
        from app.services.pasarguard import get_pg

        pg = get_pg()

    try:
        admin = await pg.get_admin(pg_username)
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
