from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Payment,
    ResellerApplication,
    ResellerApplicationStatus,
    ResellerPlan,
    ResellerProfile,
    Role,
)

# Single permission set for BOTH web panel and bot (must stay identical).
FEATURE_PERMS: list[tuple[str, str]] = [
    ("dashboard", "خانه / داشبورد"),
    ("plans", "پلن‌های فروش"),
    ("orders", "سفارش‌ها"),
    ("payments", "پرداخت‌ها و تأیید رسید"),
    ("tickets", "تیکت‌ها"),
    ("stats", "آمار و کمیسیون"),
    ("shop_settings", "تنظیمات ربات فروشگاه"),
]

DEFAULT_FEATURE_PERMS = "dashboard,plans,orders,payments,tickets,stats,shop_settings"

# Tabs a reseller may edit for their own shop bot
RESELLER_SETTINGS_TABS: list[tuple[str, str]] = [
    ("welcome", "خوش‌آمد و هویت"),
    ("appearance", "ظاهر ربات"),
    ("messages", "متن پیام‌ها"),
    ("buttons", "متن دکمه‌ها"),
    ("menu", "منوی بات"),
    ("qr", "QR اشتراک"),
    ("payment", "پرداخت"),
    ("supports", "پشتیبان‌ها"),
    ("forcejoin", "کانال اجباری"),
    ("naming", "نام‌گذاری سرویس"),
    ("notifications", "نوتیفیکیشن"),
    ("bot", "ربات اختصاصی"),
]
SETUP_TOKEN_HOURS = 48


async def get_reseller_panel_base_url(session: AsyncSession) -> str:
    """Custom reseller URL → PUBLIC_BASE_URL → http://{server_ip}:{WEB_PORT}."""
    from app.services.setup_wizard import default_panel_base_url
    from app.services.users import get_setting

    custom = (await get_setting(session, "reseller_panel_base_url") or "").strip().rstrip("/")
    if custom:
        return custom
    return default_panel_base_url()


async def get_reseller_pg_panel_base_url(session: AsyncSession) -> str:
    """Custom PG panel URL for resellers → else exact PG_BASE_URL from settings."""
    from app.config import get_settings, normalize_pg_base_url
    from app.services.users import get_setting

    custom = (await get_setting(session, "reseller_pg_panel_base_url") or "").strip()
    if custom:
        return normalize_pg_base_url(custom)
    return normalize_pg_base_url(get_settings().pg_base_url or "")


def parse_perms(raw: str | None) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    allowed = {k for k, _ in FEATURE_PERMS}
    # Map legacy bot-only keys
    legacy = {"approve_receipts": "payments"}
    out: list[str] = []
    for p in raw.replace(";", ",").split(","):
        key = legacy.get(p.strip(), p.strip())
        if key in allowed and key not in out:
            out.append(key)
    return out


def with_shop_settings(perms: list[str] | None) -> list[str]:
    """Ensure core shop capabilities are present for resellers."""
    out = list(perms or [])
    for key in ("dashboard", "shop_settings", "plans", "tickets", "orders", "payments"):
        if key not in out:
            out.append(key)
    return out


def join_perms(items: Iterable[str] | None) -> str:
    allowed = {k for k, _ in FEATURE_PERMS}
    return ",".join(sorted({str(x).strip() for x in (items or []) if str(x).strip() in allowed}))


def normalize_feature_perms(raw: str | None) -> str:
    perms = parse_perms(raw)
    if not perms:
        perms = parse_perms(DEFAULT_FEATURE_PERMS)
    return join_perms(with_shop_settings(perms))


def has_perm(profile: ResellerProfile | None, key: str, *, role: str | None = None) -> bool:
    """Unified permission check for web + bot."""
    if role == Role.ADMIN.value:
        return True
    if not profile or not profile.is_active:
        return False
    # Explicit empty string = intentionally restricted (do not soft-upgrade to DEFAULT).
    # None / unset → DEFAULT feature set. Non-empty lists always get core shop keys.
    raw = profile.web_permissions
    if raw is None:
        perms = with_shop_settings(parse_perms(DEFAULT_FEATURE_PERMS))
    else:
        parsed = parse_perms(raw)
        perms = with_shop_settings(parsed) if parsed else parsed
    return key in perms


def has_bot_perm(profile: ResellerProfile | None, key: str) -> bool:
    if key == "approve_receipts":
        key = "payments"
    return has_perm(profile, key)


def setup_is_complete(profile: ResellerProfile | None) -> bool:
    """Reseller may log into web when credentials are provisioned (or wizard finished)."""
    if not profile or not profile.is_active:
        return False
    return bool(profile.setup_completed_at and profile.web_username and profile.web_password_hash)


async def reseller_owns_user(session: AsyncSession, reseller_user_id: int, customer_user_id: int) -> bool:
    customer = await session.get(BotUser, customer_user_id)
    return bool(customer and customer.reseller_id == reseller_user_id)


async def reseller_can_review_payment(
    session: AsyncSession,
    reviewer: BotUser,
    payment: Payment,
) -> bool:
    """Shop staff on their bot: payments perm + tenancy.

    Platform admins on the main bot may only review platform-scoped payments
    (wallet top-ups / orders with no reseller_id) — never shop tenants' traffic.
    """
    from app.services.reseller_access import resolve_reseller_owner_id
    from app.services.users import current_shop_reseller_id

    shop_rid = current_shop_reseller_id()

    # Global wallet must never be mintable by a tenant reviewer.
    if payment.is_wallet_topup:
        return reviewer.role == Role.ADMIN.value and shop_rid is None

    order = None
    if payment.order_id:
        order = await session.get(Order, payment.order_id)

    # Platform admin on main bot: only non-shop orders
    if reviewer.role == Role.ADMIN.value and shop_rid is None:
        if order and order.reseller_id:
            return False
        return True

    owner_id = await resolve_reseller_owner_id(
        session,
        reviewer,
        is_reseller_bot=shop_rid is not None,
        reseller_owner_id=shop_rid,
    )
    if not owner_id:
        return False
    profile = await get_reseller_profile(session, owner_id)
    if not has_bot_perm(profile, "payments"):
        return False
    if not order or order.reseller_id != owner_id:
        return False
    return True


def _rand_password(length: int = 14) -> str:
    """PasarGuard-compatible password: ≥14 chars, ≥2 lower, ≥2 upper, ≥1 special."""
    length = max(14, int(length))
    specials = "!@#$%^&*"
    required = (
        [secrets.choice(string.ascii_lowercase) for _ in range(2)]
        + [secrets.choice(string.ascii_uppercase) for _ in range(2)]
        + [secrets.choice(specials)]
        + [secrets.choice(string.digits) for _ in range(2)]
    )
    alphabet = string.ascii_letters + string.digits + specials
    required += [secrets.choice(alphabet) for _ in range(length - len(required))]
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def _rand_username(prefix: str = "res") -> str:
    return f"{prefix}_{secrets.token_hex(3)}"


async def _unique_web_username(session: AsyncSession, prefix: str = "web") -> str:
    """Unique username usable for both web panel and Pasarguard admin."""
    from app.db.models import PgStaffAccess
    from app.services.web_auth import load_web_admin

    admin_u = (load_web_admin().get("username") or "").strip().lower()
    for _ in range(12):
        uname = _rand_username(prefix)
        if admin_u and uname == admin_u:
            continue
        clash = await session.execute(
            select(ResellerProfile).where(ResellerProfile.web_username == uname)
        )
        if clash.scalar_one_or_none() is not None:
            continue
        clash_pg = await session.execute(
            select(ResellerProfile).where(ResellerProfile.pg_admin_username == uname)
        )
        if clash_pg.scalar_one_or_none() is not None:
            continue
        clash_staff = await session.execute(
            select(PgStaffAccess).where(PgStaffAccess.web_username == uname)
        )
        if clash_staff.scalar_one_or_none() is not None:
            continue
        return uname
    return f"{prefix}_{secrets.token_hex(6)}"


async def apply_reseller_panel_password(
    session: AsyncSession,
    profile: ResellerProfile,
    password: str,
    *,
    sync_pg: bool = True,
) -> None:
    """Set web panel password and optionally the linked Pasarguard admin password (same secret)."""
    from app.services.pasarguard import get_pg, invalidate_reseller_pg_client, reset_pg
    from app.services.secret_box import encrypt_secret
    from app.services.web_auth import hash_password

    pwd = (password or "").strip()
    if not pwd:
        raise ValueError("رمز عبور الزامی است")
    profile.web_password_hash = hash_password(pwd)
    if not sync_pg or not (profile.pg_admin_username or "").strip():
        return
    profile.pg_admin_password_enc = encrypt_secret(pwd)
    try:
        await get_pg().modify_admin(profile.pg_admin_username, {"password": pwd})
    except Exception as e:
        raise ValueError(f"به‌روزرسانی رمز پاسارگارد ناموفق: {e}") from e
    # Drop cached PG clients so next shop op uses the new password
    try:
        invalidate_reseller_pg_client(int(profile.user_id))
        reset_pg()
    except Exception:
        pass


def new_setup_token() -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SETUP_TOKEN_HOURS)
    return token, expires


async def get_reseller_profile(session: AsyncSession, user_id: int) -> ResellerProfile | None:
    result = await session.execute(
        select(ResellerProfile).where(ResellerProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_profile_by_setup_token(session: AsyncSession, token: str) -> ResellerProfile | None:
    if not token or len(token) < 16:
        return None
    result = await session.execute(
        select(ResellerProfile).where(ResellerProfile.setup_token == token)
    )
    profile = result.scalar_one_or_none()
    if not profile or not profile.setup_token_expires:
        return None
    exp = profile.setup_token_expires
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return None
    return profile


async def list_active_reseller_plans(session: AsyncSession) -> list[ResellerPlan]:
    result = await session.execute(
        select(ResellerPlan)
        .where(ResellerPlan.is_active.is_(True))
        .order_by(ResellerPlan.sort_order, ResellerPlan.id)
    )
    return list(result.scalars().all())


async def list_reseller_plans(session: AsyncSession) -> list[ResellerPlan]:
    result = await session.execute(
        select(ResellerPlan).order_by(ResellerPlan.sort_order, ResellerPlan.id)
    )
    return list(result.scalars().all())


async def make_reseller(
    session: AsyncSession,
    user: BotUser,
    *,
    commission_percent: int = 10,
    can_approve_receipts: bool = False,
    pg_admin_username: str | None = None,
    pg_admin_password_enc: str | None = None,
    pg_role_id: int | None = None,
    web_username: str | None = None,
    web_password_hash: str | None = None,
    web_permissions: str | None = None,
    bot_permissions: str | None = None,
    plan_id: int | None = None,
    issue_setup_token: bool = False,
) -> ResellerProfile:
    user.role = Role.RESELLER.value
    result = await session.execute(
        select(ResellerProfile).where(ResellerProfile.user_id == user.id)
    )
    profile = result.scalar_one_or_none()
    # Force web/bot permissions identical
    perms = normalize_feature_perms(web_permissions or bot_permissions)
    if can_approve_receipts and "payments" not in parse_perms(perms):
        perms = join_perms(parse_perms(perms) + ["payments"])
    approve = "payments" in parse_perms(perms)

    if profile:
        profile.commission_percent = commission_percent
        profile.can_approve_receipts = approve
        if pg_admin_username is not None:
            profile.pg_admin_username = pg_admin_username
        if pg_admin_password_enc is not None:
            profile.pg_admin_password_enc = pg_admin_password_enc
        if pg_role_id is not None:
            from app.services.pg_access import invalidate_role_cache

            if profile.pg_role_id:
                invalidate_role_cache(int(profile.pg_role_id))
            profile.pg_role_id = pg_role_id
            invalidate_role_cache(int(pg_role_id))
        if web_username:
            profile.web_username = web_username
        if web_password_hash:
            profile.web_password_hash = web_password_hash
        profile.web_permissions = perms
        profile.bot_permissions = perms
        profile.plan_id = plan_id
        profile.is_active = True
    else:
        if pg_role_id is not None:
            from app.services.pg_access import invalidate_role_cache

            invalidate_role_cache(int(pg_role_id))
        profile = ResellerProfile(
            user_id=user.id,
            commission_percent=commission_percent,
            can_approve_receipts=approve,
            pg_admin_username=pg_admin_username,
            pg_admin_password_enc=pg_admin_password_enc,
            pg_role_id=pg_role_id,
            web_username=web_username,
            web_password_hash=web_password_hash,
            web_permissions=perms,
            bot_permissions=perms,
            plan_id=plan_id,
        )
        session.add(profile)

    if issue_setup_token:
        token, expires = new_setup_token()
        profile.setup_token = token
        profile.setup_token_expires = expires
        # Web creds already provisioned → keep login unlocked; token is for bot only
        has_web = bool(
            (web_username or profile.web_username)
            and (web_password_hash or profile.web_password_hash)
        )
        if has_web:
            profile.setup_completed_at = datetime.now(timezone.utc)
        else:
            profile.setup_completed_at = None

    await session.commit()
    await session.refresh(profile)
    return profile


async def create_application(
    session: AsyncSession,
    *,
    user: BotUser,
    plan: ResellerPlan,
    note: str | None = None,
) -> tuple[ResellerApplication, Order | None]:
    if user.role == Role.RESELLER.value:
        raise ValueError("شما هم‌اکنون نماینده هستید")
    if user.role == Role.ADMIN.value:
        raise ValueError("ادمین نیاز به درخواست نمایندگی ندارد")

    existing = await session.execute(
        select(ResellerApplication).where(
            ResellerApplication.user_id == user.id,
            ResellerApplication.status.in_(
                [
                    ResellerApplicationStatus.PENDING_PAYMENT.value,
                    ResellerApplicationStatus.AWAITING_APPROVAL.value,
                ]
            ),
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError("یک درخواست باز دارید؛ تا رسیدگی صبر کنید")

    app = ResellerApplication(
        user_id=user.id,
        plan_id=plan.id,
        note=note,
        status=(
            ResellerApplicationStatus.PENDING_PAYMENT.value
            if plan.price > 0
            else ResellerApplicationStatus.AWAITING_APPROVAL.value
        ),
    )
    session.add(app)
    await session.flush()

    order = None
    if plan.price > 0:
        order = Order(
            user_id=user.id,
            plan_id=None,
            amount=plan.price,
            status=OrderStatus.PENDING.value,
            note=f"reseller_app:{app.id}",
        )
        session.add(order)
        await session.flush()
        app.order_id = order.id

    await session.commit()
    await session.refresh(app)
    if order:
        await session.refresh(order)
    return app, order


async def get_application(session: AsyncSession, app_id: int) -> ResellerApplication | None:
    result = await session.execute(
        select(ResellerApplication)
        .options(
            selectinload(ResellerApplication.user),
            selectinload(ResellerApplication.plan),
        )
        .where(ResellerApplication.id == app_id)
    )
    return result.scalar_one_or_none()


async def list_applications(
    session: AsyncSession, *, status: str | None = None, limit: int = 100
) -> list[ResellerApplication]:
    q = (
        select(ResellerApplication)
        .options(
            selectinload(ResellerApplication.user),
            selectinload(ResellerApplication.plan),
        )
        .order_by(ResellerApplication.id.desc())
        .limit(limit)
    )
    if status:
        q = q.where(ResellerApplication.status == status)
    result = await session.execute(q)
    return list(result.scalars().all())


async def mark_application_paid(session: AsyncSession, order: Order) -> ResellerApplication | None:
    note = order.note or ""
    if not note.startswith("reseller_app:"):
        return None
    try:
        app_id = int(note.split(":", 1)[1])
    except ValueError:
        return None
    app = await session.get(ResellerApplication, app_id)
    if not app:
        return None
    if app.status == ResellerApplicationStatus.PENDING_PAYMENT.value:
        app.status = ResellerApplicationStatus.AWAITING_APPROVAL.value
        await session.commit()
        await session.refresh(app)
    return app


async def provision_reseller(
    session: AsyncSession,
    *,
    user: BotUser,
    plan: ResellerPlan | None = None,
    commission_percent: int | None = None,
    can_approve_receipts: bool | None = None,
    web_permissions: str | None = None,
    bot_permissions: str | None = None,
    create_pg_admin: bool | None = None,
    create_web_access: bool | None = None,
    share_pg_panel_url: bool | None = None,
    pg_role_id: int | None = None,
    panel_base_url: str = "",
) -> dict:
    """Activate reseller: PG admin + web panel creds + optional bot-token setup link."""
    from app.services.pasarguard import get_pg
    from app.services.web_auth import hash_password

    commission = (
        commission_percent
        if commission_percent is not None
        else (plan.commission_percent if plan else 10)
    )
    perms = normalize_feature_perms(
        web_permissions
        or bot_permissions
        or (plan.web_permissions if plan else None)
        or (plan.bot_permissions if plan else None)
    )
    if can_approve_receipts and "payments" not in parse_perms(perms):
        perms = join_perms(parse_perms(perms) + ["payments"])
    approve = "payments" in parse_perms(perms)

    role_id = pg_role_id if pg_role_id is not None else (plan.pg_role_id if plan else None)
    if create_pg_admin is not None:
        do_pg = bool(create_pg_admin)
    elif plan is not None:
        do_pg = bool(plan.create_pg_admin) or bool(role_id)
    else:
        do_pg = True

    do_web = True if create_web_access is None else bool(create_web_access)
    if share_pg_panel_url is not None:
        share_pg = bool(share_pg_panel_url)
    elif plan is not None:
        share_pg = bool(getattr(plan, "share_pg_panel_url", False))
    else:
        share_pg = False

    # One username + password for Pasarguard admin AND bot web panel (when both created).
    from app.services.secret_box import encrypt_secret

    shared_username = None
    shared_password = None
    if do_pg or do_web:
        shared_username = await _unique_web_username(session, "shop")
        shared_password = _rand_password(14)

    pg_username = None
    pg_password = None
    pg_password_enc = None
    if do_pg:
        pg_username = shared_username
        pg_password = shared_password
        pg_password_enc = encrypt_secret(pg_password)
        payload: dict = {
            "username": pg_username,
            "password": pg_password,
            "telegram_id": user.telegram_id,
            "note": f"PGClockBot reseller #{user.id}",
        }
        if role_id:
            payload["role_id"] = int(role_id)
        else:
            payload["is_sudo"] = False
        try:
            await get_pg().create_admin(payload)
        except Exception as e:
            if "role_id" in payload:
                payload.pop("role_id", None)
                payload["is_sudo"] = False
                try:
                    await get_pg().create_admin(payload)
                except Exception as e2:
                    raise ValueError(f"ساخت ادمین پاسارگارد ناموفق: {e2}") from e2
            else:
                raise ValueError(f"ساخت ادمین پاسارگارد ناموفق: {e}") from e

    web_username = None
    web_password = None
    web_hash = None
    if do_web:
        web_username = shared_username
        web_password = shared_password
        web_hash = hash_password(web_password)

    profile = await make_reseller(
        session,
        user,
        commission_percent=commission,
        can_approve_receipts=approve,
        pg_admin_username=pg_username,
        pg_admin_password_enc=pg_password_enc,
        pg_role_id=role_id,
        web_username=web_username,
        web_password_hash=web_hash,
        web_permissions=perms,
        bot_permissions=perms,
        plan_id=plan.id if plan else None,
        issue_setup_token=True,
    )
    profile.share_pg_panel_url = share_pg
    await session.commit()
    await session.refresh(profile)

    base = (panel_base_url or "").rstrip("/")
    if not base:
        base = await get_reseller_panel_base_url(session)
    setup_url = f"{base}/rsetup/{profile.setup_token}" if base and profile.setup_token else ""
    pg_panel = await get_reseller_pg_panel_base_url(session) if share_pg else ""
    unified = bool(do_pg and do_web and shared_username and shared_password)

    return {
        "profile": profile,
        "pg_username": pg_username,
        "pg_password": pg_password if (share_pg or unified) else None,
        "pg_panel_url": pg_panel,
        "share_pg_panel_url": share_pg,
        "web_username": web_username,
        "web_password": web_password,
        "panel_username": shared_username if unified else (web_username or pg_username),
        "panel_password": shared_password if unified else (web_password or pg_password),
        "unified_credentials": unified,
        "setup_url": setup_url,
        "setup_token": profile.setup_token,
        "panel_url": base,
        "commission_percent": commission,
        "permissions": perms,
    }


def format_credentials_message(creds: dict) -> str:
    """Deliver PG + web panel URLs/credentials (and optional bot setup link)."""
    from app.services.formatting import copyable

    lines = [
        "✅ <b>نمایندگی فعال شد</b>",
        "",
        f"کمیسیون شما: <b>{creds.get('commission_percent', 0)}٪</b>",
    ]

    panel = (creds.get("panel_url") or "").strip().rstrip("/")
    pg_panel = (creds.get("pg_panel_url") or "").strip().rstrip("/")
    share_pg = bool(creds.get("share_pg_panel_url")) or bool(pg_panel and creds.get("pg_username"))
    unified = bool(creds.get("unified_credentials"))
    panel_user = creds.get("panel_username") or creds.get("web_username") or creds.get("pg_username")
    panel_pass = creds.get("panel_password") or creds.get("web_password") or creds.get("pg_password")

    if unified and panel_user and panel_pass:
        lines += [
            "",
            "🔐 <b>ورود یکپارچه (وب‌پنل ربات + پاسارگارد)</b>",
            "همان یوزر و رمز برای هر دو پنل استفاده می‌شود.",
        ]
        if panel:
            lines += [
                f"آدرس وب‌پنل: {copyable(panel)}",
                f"ورود وب‌پنل: {copyable(f'{panel}/login')}",
            ]
        else:
            lines.append("آدرس وب‌پنل هنوز تنظیم نشده — از ادمین بپرسید.")
        if share_pg and pg_panel:
            lines.append(f"آدرس پاسارگارد: {copyable(pg_panel)}")
        elif not share_pg:
            lines.append(
                "لینک مستقیم پاسارگارد ارسال نشده؛ مدیریت VPN از وب‌پنل ربات هم ممکن است."
            )
        lines += [
            f"نام کاربری: {copyable(panel_user)}",
            f"رمز: {copyable(panel_pass)}",
            "رمز را عوض کنید و در جای امن نگه دارید (تغییر رمز در وب‌پنل هر دو جا را یکی نگه می‌دارد).",
        ]
    else:
        if share_pg or creds.get("pg_username"):
            lines += ["", "🛡 <b>پنل پاسارگارد</b>"]
            if pg_panel:
                lines.append(f"آدرس پنل: {copyable(pg_panel)}")
            if creds.get("pg_username") and creds.get("pg_password"):
                lines += [
                    f"نام کاربری: {copyable(creds['pg_username'])}",
                    f"رمز: {copyable(creds['pg_password'])}",
                    "رمز را عوض کنید و در جای امن نگه دارید.",
                ]
            elif not pg_panel:
                lines.append("ادمین پاسارگارد برای این پلن ساخته نشد.")
        else:
            lines += [
                "",
                "🛡 مدیریت VPN از طریق همین وب‌پنل ربات انجام می‌شود (لینک پنل پاسارگارد ارسال نشده).",
            ]

        lines += ["", "🌐 <b>وب‌پنل ربات (نماینده)</b>"]
        if panel:
            lines += [
                f"آدرس پنل: {copyable(panel)}",
                f"آدرس ورود: {copyable(f'{panel}/login')}",
            ]
        else:
            lines.append("آدرس پنل هنوز تنظیم نشده — از ادمین بپرسید.")
        if creds.get("web_username") and creds.get("web_password"):
            lines += [
                f"نام کاربری: {copyable(creds['web_username'])}",
                f"رمز: {copyable(creds['web_password'])}",
            ]

    if creds.get("setup_url"):
        lines += [
            "",
            "🤖 <b>ربات اختصاصی (اختیاری)</b>",
            "برای ثبت توکن ربات خودتان از @BotFather:",
            copyable(creds["setup_url"]),
            "لینک یک‌بارمصرف است — با کسی به اشتراک نگذارید.",
        ]

    lines += [
        "",
        "مدیریت فروشگاه فقط از <b>ربات اختصاصی</b> و <b>وب‌پنل</b> شماست — "
        "در ربات اصلی ادمین، پنل نماینده نمایش داده نمی‌شود.",
    ]
    return "\n".join(lines)


async def format_reseller_access_card(session: AsyncSession, profile) -> str:
    """Non-secret access info for a reseller on the main bot (no plaintext passwords)."""
    from app.services.formatting import copyable, format_message, info_block, kv_line

    panel = (await get_reseller_panel_base_url(session)).rstrip("/")
    lines = [
        "مدیریت فروشگاه فقط از وب‌پنل و ربات اختصاصی خودتان انجام می‌شود.",
        "در ربات اصلی ادمین، پنل عملیاتی نماینده وجود ندارد.",
        "",
    ]
    pg_u = (profile.pg_admin_username or "").strip()
    web_u = (profile.web_username or "").strip()
    unified = bool(pg_u and web_u and pg_u.lower() == web_u.lower())
    login_url = f"{panel}/login" if panel else ""
    block = [
        kv_line("🌐", "وب‌پنل", copyable(login_url) if login_url else "—"),
    ]
    if unified:
        block.append(
            kv_line("👤", "یوزر (وب‌پنل + پاسارگارد)", copyable(web_u) if web_u else "—")
        )
    else:
        block.append(kv_line("👤", "نام کاربری وب", copyable(web_u) if web_u else "—"))
        if pg_u:
            block.append(kv_line("🛡", "کاربر پاسارگارد", copyable(pg_u)))
    if profile.bot_username:
        block.append(kv_line("🤖", "ربات اختصاصی", copyable(f"@{profile.bot_username}")))
    else:
        block.append(
            kv_line("🤖", "ربات اختصاصی", "هنوز ثبت نشده — از وب‌پنل توکن را تنظیم کنید")
        )
    lines.append(info_block(block))
    lines.append("")
    if unified:
        lines.append(
            "یک یوزر/رمز برای وب‌پنل ربات و پنل پاسارگارد است. "
            "رمز فقط یک‌بار هنگام فعال‌سازی ارسال شده؛ بازنشانی از ادمین یا صفحه امنیت وب‌پنل."
        )
    else:
        lines.append(
            "رمز ورود فقط یک‌بار هنگام فعال‌سازی ارسال شده؛ در صورت فراموشی از ادمین بخواهید بازنشانی کند."
        )
    return format_message("🔐 اطلاعات نمایندگی", "\n".join(lines))


async def complete_reseller_setup(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    web_username: str | None = None,
    password_hash: str | None = None,
    bot_token: str | None = None,
    bot_username: str | None = None,
    bot_telegram_id: int | None = None,
    bot_only: bool = False,
) -> ResellerProfile:
    """Finalize setup wizard. bot_only=True updates bot token when web creds already exist."""
    if not bot_only:
        from app.services.web_auth import load_web_admin, validate_web_username

        cleaned, uerr = validate_web_username(web_username or "", lowercase=True)
        if uerr:
            raise ValueError(uerr)
        uname = cleaned
        admin_u = (load_web_admin().get("username") or "").strip().lower()
        if uname == admin_u:
            raise ValueError("این نام کاربری برای ادمین اصلی پنل رزرو است")
        clash = await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.web_username == uname,
                ResellerProfile.id != profile.id,
            )
        )
        if clash.scalar_one_or_none():
            raise ValueError("این نام کاربری قبلاً گرفته شده")
        from app.db.models import PgStaffAccess

        clash_staff = await session.execute(
            select(PgStaffAccess).where(PgStaffAccess.web_username == uname)
        )
        if clash_staff.scalar_one_or_none():
            raise ValueError("این نام کاربری قبلاً برای دسترسی وب ادمین پاسارگارد گرفته شده")
        if not password_hash:
            raise ValueError("رمز عبور الزامی است")
        profile.web_username = uname
        profile.web_password_hash = password_hash

    if bot_token:
        token = bot_token.strip()
        from app.config import get_settings

        main_token = (get_settings().bot_token or "").strip()
        if main_token and token == main_token:
            raise ValueError("نمی‌توانید توکن ربات اصلی ادمین را ثبت کنید — ربات اختصاصی بسازید")
        clash_bot = await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.bot_token == token,
                ResellerProfile.id != profile.id,
            )
        )
        if clash_bot.scalar_one_or_none():
            raise ValueError("این توکن ربات قبلاً برای نماینده دیگری ثبت شده")
        profile.bot_token = token
        profile.bot_username = (bot_username or "").lstrip("@") or None
        if bot_telegram_id:
            profile.bot_telegram_id = int(bot_telegram_id)
    elif not bot_only:
        raise ValueError("توکن ربات الزامی است")

    profile.setup_token = None
    profile.setup_token_expires = None
    profile.setup_completed_at = datetime.now(timezone.utc)
    profile.web_permissions = normalize_feature_perms(profile.web_permissions)
    profile.bot_permissions = profile.web_permissions
    profile.can_approve_receipts = "payments" in parse_perms(profile.web_permissions)
    await session.commit()
    await session.refresh(profile)
    try:
        await seed_reseller_shop_settings(session, int(profile.user_id))
    except Exception:
        pass
    try:
        from app.services.reseller_bots import clear_reseller_token_cache

        clear_reseller_token_cache()
    except Exception:
        pass
    return profile


async def seed_reseller_shop_settings(session: AsyncSession, reseller_user_id: int) -> None:
    """Seed isolated shop settings from code defaults (never live platform Setting)."""
    from app.db.models import ResellerSetting
    from app.services.users import DEFAULT_SETTINGS

    rid = int(reseller_user_id)
    existing = {
        row[0]
        for row in (
            await session.execute(
                select(ResellerSetting.key).where(ResellerSetting.reseller_user_id == rid)
            )
        ).all()
    }
    # Seed UX-critical keys so shops start independent of platform live values
    seed_keys = (
        "menu_layout",
        "menu_order",
        "shop_title",
        "welcome_text",
        "pay_card_enabled",
        "pay_gateway_enabled",
        "pay_crypto_enabled",
        "pay_wallet_enabled",
        "force_join_enabled",
        "force_join_channel",
        "notify_new_ticket",
        "show_miniapp",
    )
    added = False
    for key in seed_keys:
        if key in existing:
            continue
        if key == "menu_order":
            # Shop-safe order: never seed platform miniapp / reseller_apply
            value = "shop,services,wallet,support,referral"
        elif key == "show_miniapp":
            value = "0"
        elif key not in DEFAULT_SETTINGS:
            continue
        else:
            value = str(DEFAULT_SETTINGS[key])
        session.add(
            ResellerSetting(
                reseller_user_id=rid,
                key=key,
                value=value,
            )
        )
        added = True
    if added:
        await session.commit()
        try:
            from app.services.users import clear_settings_cache

            clear_settings_cache(rid)
        except Exception:
            pass


def _synthetic_telegram_id(pg_username: str) -> int:
    """Stable negative Telegram id for shop owners linked to an existing PG admin."""
    import zlib

    h = zlib.crc32((pg_username or "").strip().lower().encode("utf-8")) & 0xFFFFFFFF
    return -2_100_000_000_000_000 - (h % 900_000_000_000)


async def _unique_referral(session: AsyncSession) -> str:
    from app.services.users import _referral_code

    for _ in range(20):
        code = _referral_code()
        exists = (
            await session.execute(select(BotUser).where(BotUser.referral_code == code))
        ).scalar_one_or_none()
        if not exists:
            return code
    return _referral_code() + secrets.token_hex(2).upper()


async def get_or_create_pg_linked_bot_user(
    session: AsyncSession, pg_username: str
) -> BotUser:
    """BotUser that owns the shop profile for an existing PasarGuard admin."""
    from app.services.pg_staff_access import reseller_by_pg_username

    existing = await reseller_by_pg_username(session, pg_username)
    if existing:
        user = await session.get(BotUser, existing.user_id)
        if user:
            return user

    tg_id = _synthetic_telegram_id(pg_username)
    user = (
        await session.execute(select(BotUser).where(BotUser.telegram_id == tg_id))
    ).scalar_one_or_none()
    if user:
        return user

    user = BotUser(
        telegram_id=tg_id,
        username=None,
        full_name=f"PG admin {pg_username}",
        role=Role.RESELLER.value,
        referral_code=await _unique_referral(session),
    )
    session.add(user)
    await session.flush()
    return user


async def provision_existing_pg_admin(
    session: AsyncSession,
    *,
    pg_username: str,
    web_username: str,
    password: str,
    plan_id: int,
    note: str = "",
) -> tuple[ResellerProfile | None, str | None, str | None]:
    """Grant web + shop access to an existing PG admin using a reseller plan.

    Creates/updates a ResellerProfile linked to ``pg_username`` (no new PG admin).
    Removes any legacy PgStaffAccess row for the same admin.
    Returns ``(profile, setup_hint, error)``.
    """
    from app.services.pg_staff_access import (
        access_by_pg_username,
        conflict_message_for_reseller_link,
        revoke_web_access,
    )
    from app.services.pg_staff_access import resolve_pg_role_id_for_admin
    from app.services.web_auth import (
        hash_password,
        load_web_admin,
        validate_password_strength,
        validate_web_username,
    )

    pg_u = (pg_username or "").strip().lower()
    if not pg_u:
        return None, None, "نام ادمین پاسارگارد الزامی است"

    plan = await session.get(ResellerPlan, int(plan_id))
    if not plan or not plan.is_active:
        return None, None, "پلن نمایندگی معتبر نیست — ابتدا یک پلن فعال بسازید"

    cleaned, uerr = validate_web_username(web_username, lowercase=True)
    if uerr:
        return None, None, uerr

    owner_u = (load_web_admin().get("username") or "").strip().lower()
    if cleaned == owner_u:
        return None, None, "این نام کاربری برای ادمین اصلی پنل رزرو است"

    # Allow updating the reseller already linked to THIS pg admin; block other links.
    linked = (
        await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.pg_admin_username.is_not(None),
            )
        )
    ).scalars().all()
    current: ResellerProfile | None = None
    for row in linked:
        if (row.pg_admin_username or "").strip().lower() == pg_u:
            current = row
            break
    staff_row = await access_by_pg_username(session, pg_u)
    if current is None:
        conflict = await conflict_message_for_reseller_link(session, pg_u)
        # conflict_message blocks when pg_staff exists — we'll convert that path
        if conflict and staff_row is None:
            return None, None, conflict

    # Unified login: web username must match the Pasarguard admin username.
    if cleaned != pg_u:
        return (
            None,
            None,
            "برای ورود یکپارچه، نام کاربری وب باید دقیقاً همان نام ادمین پاسارگارد باشد",
        )

    # Password: empty on edit/upgrade keeps previous hash (reseller or legacy staff).
    pwd = (password or "").strip()
    if pwd:
        ok, err = validate_password_strength(pwd)
        if not ok:
            return None, None, err
        web_hash = hash_password(pwd)
    else:
        web_hash = (current.web_password_hash if current else None) or (
            staff_row.web_password_hash if staff_row else None
        )
        if not web_hash:
            return None, None, "رمز عبور الزامی است"

    # Username uniqueness (exclude current profile)
    from app.db.models import PgStaffAccess

    taken_res = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.web_username == cleaned)
        )
    ).scalar_one_or_none()
    if taken_res and (current is None or int(taken_res.id) != int(current.id)):
        return None, None, "این نام کاربری قبلاً برای یک نماینده گرفته شده"
    taken_staff = (
        await session.execute(
            select(PgStaffAccess).where(PgStaffAccess.web_username == cleaned)
        )
    ).scalar_one_or_none()
    # Same PG admin's staff row will be deleted after conversion; other rows block.
    if taken_staff and (taken_staff.pg_username or "").lower() != pg_u:
        return None, None, "این نام کاربری قبلاً برای دسترسی وب ادمین پاسارگارد گرفته شده"

    role_id = await resolve_pg_role_id_for_admin(pg_u)
    perms = normalize_feature_perms(plan.web_permissions or plan.bot_permissions)
    # Ensure core shop surfaces are always available for secondary admins
    base = parse_perms(perms)
    for must in ("dashboard", "shop_settings"):
        if must not in base:
            base.append(must)
    perms = join_perms(base)

    user = await get_or_create_pg_linked_bot_user(session, pg_u)
    profile = await make_reseller(
        session,
        user,
        commission_percent=int(plan.commission_percent or 0),
        can_approve_receipts="payments" in parse_perms(perms),
        pg_admin_username=pg_u,
        pg_role_id=role_id,
        web_username=cleaned,
        web_password_hash=web_hash,
        web_permissions=perms,
        bot_permissions=perms,
        plan_id=int(plan.id),
        issue_setup_token=True,
    )
    if note:
        # Store operator note on profile via unused field if present; else ignore
        pass
    profile.is_active = True
    # Keep Pasarguard password in sync when operator sets a new shared password
    if pwd:
        from app.services.pasarguard import get_pg, invalidate_reseller_pg_client, reset_pg
        from app.services.secret_box import encrypt_secret

        profile.pg_admin_password_enc = encrypt_secret(pwd)
        try:
            await get_pg().modify_admin(pg_u, {"password": pwd})
            invalidate_reseller_pg_client(int(profile.user_id))
            reset_pg()
        except Exception as e:
            return None, None, f"به‌روزرسانی رمز پاسارگارد ناموفق: {e}"
    await session.commit()
    await session.refresh(profile)

    # Convert legacy pg_staff row away so one-path invariant holds
    try:
        await revoke_web_access(session, pg_u)
    except Exception:
        pass

    setup_hint = None
    if not (profile.bot_token or "").strip():
        setup_hint = "shop-settings?tab=bot"
    return profile, setup_hint, None


def bot_needs_setup(profile: ResellerProfile | None) -> bool:
    """True when reseller/shop owner has web access but no dedicated Telegram bot yet."""
    if not profile or not profile.is_active:
        return False
    return not bool((profile.bot_token or "").strip())


async def approve_application(
    session: AsyncSession,
    app: ResellerApplication,
    *,
    reviewer_tg: int | None,
    panel_base_url: str = "",
    admin_note: str | None = None,
) -> dict:
    if app.status not in {
        ResellerApplicationStatus.AWAITING_APPROVAL.value,
        ResellerApplicationStatus.PENDING_PAYMENT.value,
    }:
        raise ValueError("این درخواست قابل تأیید نیست")
    user = await session.get(BotUser, app.user_id)
    plan = await session.get(ResellerPlan, app.plan_id)
    if not user or not plan:
        raise ValueError("کاربر یا پلن یافت نشد")
    if app.status == ResellerApplicationStatus.PENDING_PAYMENT.value and plan.price > 0:
        raise ValueError("هنوز پرداخت این درخواست تکمیل نشده")

    creds = await provision_reseller(
        session,
        user=user,
        plan=plan,
        panel_base_url=panel_base_url,
    )
    app.status = ResellerApplicationStatus.APPROVED.value
    app.reviewed_by = reviewer_tg
    if admin_note:
        app.admin_note = admin_note
    await session.commit()
    return creds


async def reject_application(
    session: AsyncSession,
    app: ResellerApplication,
    *,
    reviewer_tg: int | None,
    admin_note: str | None = None,
) -> None:
    if app.status in {
        ResellerApplicationStatus.APPROVED.value,
        ResellerApplicationStatus.REJECTED.value,
    }:
        raise ValueError("وضعیت درخواست قابل تغییر نیست")
    app.status = ResellerApplicationStatus.REJECTED.value
    app.reviewed_by = reviewer_tg
    if admin_note:
        app.admin_note = admin_note
    await session.commit()


async def revoke_reseller(
    session: AsyncSession,
    user_id: int,
    *,
    delete_pg_admin: bool = True,
    commit: bool = True,
    reason: str | None = None,
) -> dict:
    """Remove reseller profile, unlink customers, demote role to user.

    Does not delete the BotUser row. Optional PasarGuard admin cleanup.
    """
    from sqlalchemy import update

    from app.services.pasarguard import get_pg, invalidate_reseller_pg_client

    user = await session.get(BotUser, user_id)
    if not user:
        raise ValueError("کاربر یافت نشد")
    profile = await get_reseller_profile(session, user_id)
    if not profile:
        raise ValueError("این کاربر نماینده نیست")

    pg_username = (profile.pg_admin_username or "").strip() or None
    profile_id = profile.id
    pg_deleted = False
    if delete_pg_admin and pg_username:
        try:
            await get_pg().delete_admin(pg_username)
            pg_deleted = True
        except Exception:
            pg_deleted = False

    await session.execute(
        update(BotUser).where(BotUser.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(Order).where(Order.reseller_id == user_id).values(reseller_id=None)
    )
    from app.db.models import Ticket

    await session.execute(
        update(Ticket).where(Ticket.reseller_id == user_id).values(reseller_id=None)
    )

    await session.delete(profile)
    if user.role == Role.RESELLER.value:
        user.role = Role.USER.value

    if commit:
        await session.commit()
        await session.refresh(user)

    # Drop cached shop PG client + web gate so revoked credentials cannot linger.
    try:
        invalidate_reseller_pg_client(int(user_id))
    except Exception:
        pass
    if pg_username:
        try:
            from app.services.pg_staff_access import invalidate_pg_gate_cache

            invalidate_pg_gate_cache(pg_username)
        except Exception:
            pass

    try:
        from app.services.reseller_bots import get_reseller_bot_manager

        mgr = get_reseller_bot_manager()
        if mgr:
            await mgr.stop_reseller(profile_id)
    except Exception:
        pass

    return {
        "user_id": user_id,
        "telegram_id": user.telegram_id,
        "pg_admin_username": pg_username,
        "pg_admin_deleted": pg_deleted,
        "reason": (reason or "").strip() or None,
    }


def format_revoke_message(reason: str) -> str:
    """Notify former reseller that their agency access was removed."""
    from app.services.notifications import format_account_edit_subject

    return format_account_edit_subject(event="reseller_revoke", reason=reason)


async def notify_reseller_revoked(
    telegram_id: int,
    reason: str,
    *,
    session: AsyncSession | None = None,
    user: BotUser | None = None,
    actor: str | None = None,
) -> bool:
    """Best-effort Telegram notice after revoke. Returns True if subject was notified."""
    from app.services.notifications import (
        format_account_edit_subject,
        notify_account_edit,
    )

    if session is not None and user is not None:
        result = await notify_account_edit(
            session,
            user=user,
            event="reseller_revoke",
            reason=reason,
            new_role=Role.USER.value,
            old_role=Role.RESELLER.value,
            actor=actor,
            notify_subject=True,
        )
        return bool(result.get("subject"))

    # Legacy path (telegram_id only) — subject message, no admin mirror
    try:
        from app.bot import create_bot

        bot = create_bot()
        try:
            await bot.send_message(
                telegram_id,
                format_account_edit_subject(event="reseller_revoke", reason=reason),
                parse_mode="HTML",
            )
            return True
        finally:
            await bot.session.close()
    except Exception:
        return False
