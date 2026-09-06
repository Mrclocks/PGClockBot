from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)

# Single permission set for BOTH web panel and bot (must stay identical).
FEATURE_PERMS: list[tuple[str, str]] = [
    ("dashboard", "خانه / داشبورد"),
    ("plans", "پلن‌های فروش"),
    ("orders", "سفارش‌ها"),
    ("payments", "پرداخت‌ها و تأیید رسید"),
    ("tickets", "تیکت‌ها"),
    ("stats", "آمار"),
    ("shop_settings", "تنظیمات ربات فروشگاه"),
    ("loyalty", "باشگاه مشتریان"),
]

DEFAULT_FEATURE_PERMS = "dashboard,plans,orders,payments,tickets,stats,shop_settings,loyalty"

# Tabs a reseller may edit for their own shop bot
RESELLER_SETTINGS_TABS: list[tuple[str, str]] = [
    ("welcome", "خوش‌آمد و هویت"),
    ("appearance", "هویت ربات"),
    ("messages", "متن پیام‌ها"),
    ("buttons", "متن دکمه‌ها"),
    ("colors", "رنگبندی دکمه‌ها"),
    ("menu", "منوی بات"),
    ("links", "لینک‌های سریع"),
    ("qr", "QR اشتراک"),
    ("forcejoin", "کانال اجباری"),
    ("terms", "قوانین"),
    ("naming", "نام‌گذاری سرویس"),
    ("notifications", "نوتیفیکیشن"),
    ("daily_report", "گزارش روزانه"),
    ("bot", "ربات اختصاصی"),
]

# Legacy shop-settings tabs moved to page modals
SHOP_SETTINGS_DOMAIN_REDIRECTS: dict[str, str] = {
    "payment": "/finance?tab=orders&settings=payment",
    "supports": "/tickets?supports=1",
    "loyalty": "/loyalty?settings=referral",
}
SHOP_SETTINGS_DOMAIN_POST_TABS = frozenset(SHOP_SETTINGS_DOMAIN_REDIRECTS.keys())


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
    """Unified permission check for web + bot (authz decision layer)."""
    from app.services.authz import shop_feature_allowed

    return shop_feature_allowed(key=key, profile=profile, role=role)


def has_bot_perm(profile: ResellerProfile | None, key: str, *, role: str | None = None) -> bool:
    """Bot menu/action check — identical to web shop ACL (``web_permissions``).

    Phase D4 Q3: ``bot_permissions`` is a mirrored DB column only; decisions
    never read it (keep writing both columns in sync).
    """
    return has_perm(profile, key, role=role)


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
    """PasarGuard-compatible password (Phase D1 shared generator)."""
    from app.services.credential_policy import generate_compliant_password

    return generate_compliant_password(length)


async def apply_reseller_panel_password(
    session: AsyncSession,
    profile: ResellerProfile,
    password: str,
    *,
    sync_pg: bool = True,
) -> None:
    """Set web panel password and optionally the linked Pasarguard admin password (same secret)."""
    from app.services.credential_policy import validate_password_strength
    from app.services.pasarguard import get_pg, reset_pg
    from app.services.secret_box import encrypt_secret
    from app.services.web_auth import hash_password

    pwd = (password or "").strip()
    if not pwd:
        raise ValueError("رمز عبور الزامی است")
    ok, err = validate_password_strength(pwd, username=profile.web_username or profile.pg_admin_username)
    if not ok:
        raise ValueError(err)
    profile.web_password_hash = hash_password(pwd)
    if not sync_pg or not (profile.pg_admin_username or "").strip():
        return
    enc = encrypt_secret(pwd)
    if not enc:
        raise ValueError("رمز‌گذاری رمز پاسارگارد ناموفق بود — دوباره تلاش کنید")
    from app.services.org_principals import get_principal_by_reseller_profile

    linked = await get_principal_by_reseller_profile(session, int(profile.id))
    if (
        linked is not None
        and str(getattr(linked, "status", "") or "") == "active"
        and (linked.pg_password_enc or "").strip()
        and int(getattr(linked, "depth", -1) or -1) in (1, 2)
    ):
        from app.services.pasarguard import get_pg_for_principal, invalidate_pg_principal_cache

        try:
            client = await get_pg_for_principal(session, principal_id=int(linked.id))
            await client.modify_admin(profile.pg_admin_username, {"password": pwd})
        except Exception as e:
            raise ValueError(f"به‌روزرسانی رمز پاسارگارد ناموفق: {e}") from e
        linked.pg_password_enc = enc
        profile.pg_admin_password_enc = None
        try:
            invalidate_pg_principal_cache(int(linked.id))
        except Exception:
            pass
        return
    if (profile.pg_admin_password_enc or "").strip():
        from app.services.pasarguard import get_pg_for_reseller, invalidate_pg_reseller_cache

        try:
            client = await get_pg_for_reseller(session, int(profile.user_id))
            await client.modify_admin(profile.pg_admin_username, {"password": pwd})
        except Exception as e:
            raise ValueError(f"به‌روزرسانی رمز پاسارگارد ناموفق: {e}") from e
        profile.pg_admin_password_enc = enc
        try:
            invalidate_pg_reseller_cache(int(profile.user_id))
        except Exception:
            pass
        return
    # Owner-panel shop with no stored tenant secret — Owner provisioning only.
    profile.pg_admin_password_enc = enc
    try:
        await get_pg().modify_admin(profile.pg_admin_username, {"password": pwd})
    except Exception as e:
        raise ValueError(f"به‌روزرسانی رمز پاسارگارد ناموفق: {e}") from e
    # Drop cached PG clients so next shop op uses the new password
    try:
        reset_pg()
    except Exception:
        pass


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


async def get_reseller_profile(session: AsyncSession, user_id: int) -> ResellerProfile | None:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(ResellerProfile)
        .options(selectinload(ResellerProfile.plan))
        .where(ResellerProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_active_reseller_plans(
    session: AsyncSession,
    *,
    billing_mode: str | None = None,
    plan_kind: str | None = "subscription",
) -> list[ResellerPlan]:
    """Active reseller packages. Optionally filter by ``fixed`` / ``payg`` and kind.

    Default ``plan_kind=subscription`` so apply / grant lists exclude addon packs.
    Pass ``plan_kind=None`` to include every kind.
    """
    from app.services.billing import BILLING_MODE_FIXED, BILLING_MODE_PAYG

    q = select(ResellerPlan).where(ResellerPlan.is_active.is_(True))
    if plan_kind is not None:
        q = q.where(ResellerPlan.plan_kind == str(plan_kind))
    if billing_mode is not None:
        mode = str(billing_mode).strip().lower()
        if mode not in {BILLING_MODE_FIXED, BILLING_MODE_PAYG}:
            mode = BILLING_MODE_FIXED
        q = q.where(ResellerPlan.billing_mode == mode)
    result = await session.execute(q.order_by(ResellerPlan.sort_order, ResellerPlan.id))
    return list(result.scalars().all())


def normalize_reseller_billing_mode(raw: str | None) -> str:
    from app.services.billing import BILLING_MODE_FIXED, BILLING_MODE_PAYG

    mode = str(raw or "").strip().lower()
    return mode if mode in {BILLING_MODE_FIXED, BILLING_MODE_PAYG} else BILLING_MODE_FIXED


def reseller_billing_mode_label(mode: str | None) -> str:
    from app.services.billing import BILLING_MODE_PAYG

    return "PAYG" if normalize_reseller_billing_mode(mode) == BILLING_MODE_PAYG else "ثابت"


def reseller_plan_mode_of(plan: ResellerPlan | None) -> str:
    return normalize_reseller_billing_mode(getattr(plan, "billing_mode", None) if plan else None)


def format_reseller_plan_apply_detail(plan: ResellerPlan, *, currency: str) -> str:
    """Type-specific Persian body for apply / review screens."""
    import html

    from app.services.billing import BILLING_MODE_PAYG
    from app.services.formatting import format_toman
    from app.services.pg_admin_subscription import is_addon_plan, plan_kind_of

    desc = html.escape((plan.description or "").strip() or "بدون توضیح")
    name = html.escape((plan.name or "").strip() or "—")
    if is_addon_plan(plan):
        kind = plan_kind_of(plan)
        if kind == "addon_users":
            qty = int(getattr(plan, "addon_users", 0) or 0)
            qty_line = f"تعداد کاربر: <b>+{qty}</b>"
            kind_label = "بسته کاربر"
        else:
            qty = int(getattr(plan, "addon_gb", 0) or 0)
            qty_line = f"حجم: <b>+{qty} گیگ</b>"
            kind_label = "بسته حجم"
        return "\n".join(
            [
                f"<b>{name}</b>",
                desc,
                "",
                f"نوع: <b>{kind_label}</b>",
                qty_line,
                f"قیمت بسته: <b>{format_toman(plan.price, currency) if plan.price else 'رایگان'}</b>",
                "بدون گروه/نقش/نام‌گذاری سرویس — فقط برای نمایندگان با اشتراک فعال.",
            ]
        )

    mode = reseller_plan_mode_of(plan)
    lines = [
        f"<b>{name}</b>",
        desc,
        "",
        f"نوع: <b>{reseller_billing_mode_label(mode)}</b>",
        f"قیمت ورود: <b>{format_toman(plan.price, currency) if plan.price else 'رایگان'}</b>",
    ]
    if mode == BILLING_MODE_PAYG:
        rate = int(getattr(plan, "price_per_gb", 0) or 0)
        lines.append(
            f"نرخ مصرف: <b>{format_toman(rate, currency) if rate else '—'} / گیگ</b>"
        )
    else:
        if bool(getattr(plan, "allow_buy_extra", False)):
            eg = int(getattr(plan, "extra_gb_price", 0) or 0)
            eu = int(getattr(plan, "extra_user_price", 0) or 0)
            lines.append(
                "خرید اضافه: <b>فعال</b> "
                f"(گیگ {format_toman(eg, currency) if eg else '—'} · "
                f"کاربر {format_toman(eu, currency) if eu else '—'})"
            )
    groups = (getattr(plan, "pg_group_ids", None) or "").strip()
    lines.append(
        f"گروه‌های پاسارگارد: <code>{html.escape(groups) if groups else '—'}</code>"
    )
    role_id = getattr(plan, "pg_role_id", None)
    lines.append(
        f"نقش پاسارگارد: <code>{html.escape(str(role_id)) if role_id else '—'}</code>"
    )
    return "\n".join(lines)


async def list_reseller_plans(session: AsyncSession) -> list[ResellerPlan]:
    result = await session.execute(
        select(ResellerPlan).order_by(ResellerPlan.sort_order, ResellerPlan.id)
    )
    return list(result.scalars().all())


async def make_reseller(
    session: AsyncSession,
    user: BotUser,
    *,
    can_approve_receipts: bool = False,
    pg_admin_username: str | None = None,
    pg_admin_password_enc: str | None = None,
    pg_role_id: int | None = None,
    web_username: str | None = None,
    web_password_hash: str | None = None,
    web_permissions: str | None = None,
    bot_permissions: str | None = None,
    plan_id: int | None = None,
    billing_mode: str | None = None,
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
    mode = (billing_mode or "").strip().lower()
    if mode not in {"fixed", "payg"}:
        mode = None

    if profile:
        profile.can_approve_receipts = approve
        if mode:
            profile.billing_mode = mode
        if pg_admin_username is not None:
            profile.pg_admin_username = pg_admin_username
        if pg_admin_password_enc is not None:
            profile.pg_admin_password_enc = pg_admin_password_enc
        if pg_role_id is not None:
            profile.pg_role_id = pg_role_id
        if web_username:
            profile.web_username = web_username
        if web_password_hash:
            profile.web_password_hash = web_password_hash
        profile.web_permissions = perms
        profile.bot_permissions = perms
        profile.plan_id = plan_id
        profile.is_active = True
    else:
        profile = ResellerProfile(
            user_id=user.id,
            can_approve_receipts=approve,
            billing_mode=mode or "fixed",
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
        # Legacy flag: web credentials unlock the panel; bot token is set in web dashboard.
        # Do not mint one-time /rsetup links anymore.
        has_web = bool(
            (web_username or profile.web_username)
            and (web_password_hash or profile.web_password_hash)
        )
        if has_web:
            profile.setup_completed_at = datetime.now(timezone.utc)
            profile.setup_token = None
            profile.setup_token_expires = None
        else:
            profile.setup_completed_at = None
    elif profile.web_username and profile.web_password_hash and not profile.setup_completed_at:
        profile.setup_completed_at = datetime.now(timezone.utc)
        profile.setup_token = None
        profile.setup_token_expires = None

    await session.flush()
    from app.services.org_principals import bind_reseller_profile_principal

    # Fresh install: map this shop to its depth-1 Principal now (idempotent).
    await bind_reseller_profile_principal(session, profile)
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

    from app.services.billing import BillingError, assert_payg_purchase_wallet

    if reseller_plan_mode_of(plan) == "payg":
        try:
            await assert_payg_purchase_wallet(session, int(user.wallet_balance or 0))
        except BillingError as e:
            raise ValueError(e.message) from e

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
    """Mark reseller application paid, then auto-approve (provision) when possible.

    After payment, plan + agency are activated in one step — no second admin click.
    On provision failure the app stays ``awaiting_approval`` for manual retry.
    Plaintext credentials (when provisioned) are attached as
    ``order._reseller_app_creds`` for the delivery / notify layer.
    """
    import logging

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

    if app.status == ResellerApplicationStatus.AWAITING_APPROVAL.value:
        try:
            base = await get_reseller_panel_base_url(session)
            creds = await approve_application(
                session,
                app,
                reviewer_tg=None,
                panel_base_url=base,
                admin_note="auto-approved after payment",
            )
            await session.refresh(app)
            if isinstance(creds, dict):
                order.__dict__["_reseller_app_creds"] = creds
        except Exception:
            logging.getLogger(__name__).exception(
                "auto-approve reseller application %s after payment failed", app.id
            )
    return app


async def provision_reseller(
    session: AsyncSession,
    *,
    user: BotUser,
    plan: ResellerPlan | None = None,
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

    plan_billing = str(getattr(plan, "billing_mode", None) or "fixed").strip().lower()
    if plan_billing not in {"fixed", "payg"}:
        plan_billing = "fixed"
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
        if not pg_password_enc:
            raise ValueError("رمز‌گذاری رمز پاسارگارد ناموفق بود — دوباره تلاش کنید")
        payload: dict = {
            "username": pg_username,
            "password": pg_password,
            "note": f"PGClockBot reseller #{user.id}",
        }
        from app.services.platform_identity import deliverable_telegram_id

        tid = deliverable_telegram_id(getattr(user, "telegram_id", None))
        if tid is not None:
            payload["telegram_id"] = tid
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
        can_approve_receipts=approve,
        pg_admin_username=pg_username,
        pg_admin_password_enc=pg_password_enc,
        pg_role_id=role_id,
        web_username=web_username,
        web_password_hash=web_hash,
        web_permissions=perms,
        bot_permissions=perms,
        plan_id=plan.id if plan else None,
        billing_mode=plan_billing,
        issue_setup_token=True,
    )
    profile.share_pg_panel_url = share_pg
    await session.commit()
    await session.refresh(profile)

    if plan is not None and pg_username:
        try:
            from app.services.pg_admin_subscription import (
                is_subscription_plan,
                start_or_refresh_subscription,
            )

            if is_subscription_plan(plan):
                await start_or_refresh_subscription(
                    session,
                    pg_username=pg_username,
                    plan=plan,
                    reset_extras=True,
                    apply_pg_limits=True,
                )
                await session.commit()
        except Exception:
            logger.exception(
                "subscription start failed after provision reseller pg=%s", pg_username
            )

    base = (panel_base_url or "").rstrip("/")
    if not base:
        base = await get_reseller_panel_base_url(session)
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
        "panel_url": base,
        "permissions": perms,
        "plan_name": (plan.name if plan else None) or None,
        "billing_mode": plan_billing,
    }


def format_credentials_message(creds: dict) -> str:
    """Short, sectioned delivery of panel credentials (no one-time bot setup link)."""
    import html

    from app.services.formatting import copyable

    plan_name = html.escape(str(creds.get("plan_name") or "").strip())
    billing = str(creds.get("billing_mode") or "").strip().lower()

    lines: list[str] = [
        "✅ <b>نمایندگی فعال شد</b>",
        "",
    ]
    if plan_name:
        lines.append(f"📦 <b>پلن</b>\n{plan_name}")
        lines.append("")
    if billing == "payg":
        lines.append("💳 <b>نوع</b>\nPAYG")
    else:
        lines.append("📦 <b>نوع</b>\nثابت")
    lines.append("")

    panel = (creds.get("panel_url") or "").strip().rstrip("/")
    pg_panel = (creds.get("pg_panel_url") or "").strip().rstrip("/")
    share_pg = bool(creds.get("share_pg_panel_url")) or bool(pg_panel and creds.get("pg_username"))
    unified = bool(creds.get("unified_credentials"))
    panel_user = creds.get("panel_username") or creds.get("web_username") or creds.get("pg_username")
    panel_pass = creds.get("panel_password") or creds.get("web_password") or creds.get("pg_password")

    if unified and panel_user and panel_pass:
        lines += [
            "🔐 <b>ورود یکپارچه</b>",
            "<i>همان یوزر و رمز برای وب‌پنل و پاسارگارد</i>",
            "",
        ]
        if panel:
            lines += [f"🌐 <b>آدرس وب‌پنل</b>\n{copyable(panel)}", ""]
        else:
            lines += ["🌐 <b>آدرس وب‌پنل</b>\nهنوز تنظیم نشده — از ادمین بپرسید.", ""]
        if share_pg and pg_panel:
            lines += [f"🛡 <b>آدرس پاسارگارد</b>\n{copyable(pg_panel)}", ""]
        lines += [
            f"👤 <b>نام کاربری</b>\n{copyable(panel_user)}",
            "",
            f"🔑 <b>رمز عبور</b>\n{copyable(panel_pass)}",
            "",
            "💡 رمز را عوض کنید و در جای امن نگه دارید.",
        ]
    else:
        if share_pg or creds.get("pg_username"):
            lines += ["🛡 <b>پنل پاسارگارد</b>", ""]
            if pg_panel:
                lines += [f"آدرس:\n{copyable(pg_panel)}", ""]
            if creds.get("pg_username") and creds.get("pg_password"):
                lines += [
                    f"👤 نام کاربری:\n{copyable(creds['pg_username'])}",
                    "",
                    f"🔑 رمز:\n{copyable(creds['pg_password'])}",
                    "",
                ]
            elif not pg_panel:
                lines += ["ادمین پاسارگارد برای این پلن ساخته نشد.", ""]

        lines += ["🌐 <b>وب‌پنل ربات</b>", ""]
        if panel:
            lines += [f"آدرس:\n{copyable(panel)}", ""]
        else:
            lines += ["آدرس هنوز تنظیم نشده — از ادمین بپرسید.", ""]
        if creds.get("web_username") and creds.get("web_password"):
            lines += [
                f"👤 نام کاربری:\n{copyable(creds['web_username'])}",
                "",
                f"🔑 رمز:\n{copyable(creds['web_password'])}",
                "",
            ]

    lines.append("از داشبورد وب‌پنل می‌توانید ربات اختصاصی فروشگاه را تنظیم کنید.")
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
        "pay_psp_enabled",
        "pay_card_auto_enabled",
        "pay_crypto_enabled",
        "pay_wallet_enabled",
        "force_join_enabled",
        "force_join_channel",
        "force_join_msg",
        "btn_force_join",
        "btn_force_join_check",
        "terms_entry_enabled",
        "terms_entry_text",
        "terms_entry_btn",
        "terms_entry_reaccept",
        "terms_buy_user_enabled",
        "terms_buy_user_text",
        "terms_buy_user_btn",
        "terms_buy_user_reaccept",
        "terms_buy_reseller_enabled",
        "terms_buy_reseller_text",
        "terms_buy_reseller_btn",
        "terms_buy_reseller_reaccept",
        "notify_new_ticket",
        "show_miniapp",
        "admin_daily_report_enabled",
        "admin_daily_report_hour",
        "admin_daily_report_template",
        "admin_daily_report_metrics",
    )
    added = False
    for key in seed_keys:
        if key in existing:
            continue
        if key == "menu_order":
            # Shop-safe order: never seed platform miniapp / reseller_apply
            value = "shop,services,wallet,support,loyalty"
        elif key == "show_miniapp":
            value = "0"
        elif key == "admin_daily_report_enabled":
            # Shops opt in — do not spam by default.
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


def _synthetic_telegram_id(pg_username: str, *, salt: int = 0) -> int:
    """Stable negative Telegram id for shop owners linked to an existing PG admin.

    Uses CRC32 for backward-compatible ids. Optional *salt* disambiguates rare
    collisions without reshuffling existing mappings (salt=0 preserves legacy).

    Phase D4: these ids are internal — ``is_synthetic_telegram_id`` / notify
    paths must not treat them as real Telegram users.
    """
    import hashlib
    import zlib

    key = (pg_username or "").strip().lower()
    if salt:
        # Disambiguation path only — never used for first mapping of a username
        digest = hashlib.sha256(f"{key}:{salt}".encode("utf-8")).digest()
        h = int.from_bytes(digest[:8], "big")
        return -2_100_000_000_000_000 - (h % 900_000_000_000)
    h = zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF
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
        # Collision guard: another PG admin may already own this synthetic id
        linked = (
            await session.execute(
                select(ResellerProfile).where(ResellerProfile.user_id == user.id)
            )
        ).scalar_one_or_none()
        other = (linked.pg_admin_username or "").strip().lower() if linked else ""
        mine = (pg_username or "").strip().lower()
        if not other or other == mine:
            return user
        # Rare CRC32 collision — find an unused salted id
        for salt in range(1, 32):
            cand = _synthetic_telegram_id(pg_username, salt=salt)
            clash = (
                await session.execute(select(BotUser).where(BotUser.telegram_id == cand))
            ).scalar_one_or_none()
            if clash is None:
                tg_id = cand
                user = None
                break
        else:
            raise RuntimeError("امکان تخصیص شناسه پایدار برای ادمین پاسارگارد نیست")
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
    web_permissions: str | None = None,
    share_pg_panel_url: bool | None = None,
) -> tuple[ResellerProfile | None, str | None, str | None]:
    """Grant web + shop access to an existing PG admin using a reseller plan.

    Creates/updates a ResellerProfile linked to ``pg_username`` (no new PG admin).
    Refuses when a PgStaffAccess row exists (Phase D2 — no automatic conversion).
    Optional ``web_permissions`` overrides the plan defaults (same keys as create-reseller).
    Returns ``(profile, setup_hint, error)``.
    """
    from app.services.pg_staff_access import (
        access_by_pg_username,
        conflict_message_for_reseller_link,
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

    # Phase D2: never silently convert pg_staff → reseller
    staff_row = await access_by_pg_username(session, pg_u)
    if staff_row is not None:
        return (
            None,
            None,
            "این ادمین دسترسی ادمین فرعی دارد — ابتدا آن را حذف کنید؛ "
            "تبدیل خودکار به نماینده مجاز نیست. "
            "علت: هر ادمین فقط یکی از مسیرهای ادمین فرعی یا نماینده را می‌تواند داشته باشد. "
            "راه حل: از «ادمین‌ها» دسترسی ادمین فرعی را لغو کنید، سپس اعطای نماینده را انجام دهید.",
        )

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
    if current is None:
        conflict = await conflict_message_for_reseller_link(session, pg_u)
        if conflict:
            return None, None, conflict

    # Unified login: web username must match the Pasarguard admin username.
    if cleaned != pg_u:
        return (
            None,
            None,
            "برای ورود یکپارچه، نام کاربری وب باید دقیقاً همان نام ادمین پاسارگارد باشد",
        )

    # Password: empty on edit keeps previous hash only when enc already present.
    pwd = (password or "").strip()
    if pwd:
        ok, err = validate_password_strength(pwd, username=cleaned)
        if not ok:
            return None, None, err
        web_hash = hash_password(pwd)
    else:
        web_hash = current.web_password_hash if current else None
        if not web_hash:
            return None, None, "رمز عبور الزامی است"
        from app.services.secret_box import decrypt_secret

        if current and not decrypt_secret(current.pg_admin_password_enc):
            return (
                None,
                None,
                "رمز عبور الزامی است — اعتبارنامه پاسارگارد ذخیره نشده",
            )

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
    if taken_staff:
        return None, None, "این نام کاربری قبلاً برای دسترسی وب ادمین پاسارگارد گرفته شده"

    role_id = await resolve_pg_role_id_for_admin(pg_u)
    perms = normalize_feature_perms(
        web_permissions or plan.web_permissions or plan.bot_permissions
    )
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
        can_approve_receipts="payments" in parse_perms(perms),
        pg_admin_username=pg_u,
        pg_role_id=role_id,
        web_username=cleaned,
        web_password_hash=web_hash,
        web_permissions=perms,
        bot_permissions=perms,
        plan_id=int(plan.id),
        billing_mode=str(getattr(plan, "billing_mode", None) or "fixed"),
        issue_setup_token=True,
    )
    if note:
        # Store operator note on profile via unused field if present; else ignore
        pass
    profile.is_active = True
    if share_pg_panel_url is not None:
        profile.share_pg_panel_url = bool(share_pg_panel_url)
    elif getattr(plan, "share_pg_panel_url", None) is not None:
        profile.share_pg_panel_url = bool(plan.share_pg_panel_url)
    # Keep Pasarguard password in sync when operator sets a new shared password
    if pwd:
        from app.services.pasarguard import get_pg, reset_pg
        from app.services.secret_box import encrypt_secret

        enc = encrypt_secret(pwd)
        if not enc:
            return None, None, "رمز‌گذاری رمز پاسارگارد ناموفق بود — دوباره تلاش کنید"
        profile.pg_admin_password_enc = enc
        try:
            await get_pg().modify_admin(pg_u, {"password": pwd})
            reset_pg()
        except Exception as e:
            return None, None, f"به‌روزرسانی رمز پاسارگارد ناموفق: {e}"
    await session.commit()
    await session.refresh(profile)

    try:
        from app.services.pg_admin_subscription import (
            is_subscription_plan,
            start_or_refresh_subscription,
        )

        if is_subscription_plan(plan):
            await start_or_refresh_subscription(
                session,
                pg_username=pg_u,
                plan=plan,
                reset_extras=current is None,
                apply_pg_limits=True,
            )
            await session.commit()
    except Exception:
        logger.exception("subscription start failed after provision_existing pg=%s", pg_u)

    setup_hint = None
    if not (profile.bot_token or "").strip():
        setup_hint = "shop-settings?tab=bot"
    return profile, setup_hint, None


async def convert_staff_to_reseller(
    session: AsyncSession,
    *,
    pg_username: str,
    plan_id: int,
    password: str = "",
    note: str = "",
) -> tuple[ResellerProfile | None, str | None, str | None]:
    """Owner-approved explicit migration: pg_staff → reseller (one web path).

    Removes the staff row first, then provisions reseller with the same PG admin.
    Does not create a second grant; keeps fail-closed credential/quota isolation.
    """
    from app.services.pg_staff_access import access_by_pg_username, revoke_web_access
    from app.services.secret_box import decrypt_secret

    pg_u = (pg_username or "").strip().lower()
    if not pg_u:
        return None, None, "نام ادمین پاسارگارد الزامی است"

    staff = await access_by_pg_username(session, pg_u)
    if staff is None:
        return None, None, "دسترسی ادمین فرعی برای این ادمین وجود ندارد"

    pwd = (password or "").strip()
    if not pwd:
        pwd = (decrypt_secret(staff.pg_admin_password_enc) or "").strip()
    if not pwd:
        return (
            None,
            None,
            "رمز عبور الزامی است. "
            "علت: اعتبارنامه پاسارگارد برای ادمین فرعی ذخیره نشده. "
            "راه حل: رمز مشترک را در فرم وارد کنید.",
        )

    web_u = (staff.web_username or pg_u).strip().lower() or pg_u
    # Drop staff row without committing so a failed provision can roll back.
    await revoke_web_access(session, pg_u, commit=False)

    profile, hint, err = await provision_existing_pg_admin(
        session,
        pg_username=pg_u,
        web_username=web_u,
        password=pwd,
        plan_id=plan_id,
        note=note or "converted from pg_staff",
    )
    if err:
        # Staff delete was flushed but not committed; restore prior state.
        await session.rollback()
        return None, None, f"تبدیل به نماینده ناموفق بود: {err}"
    return profile, hint, None


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

    from app.services.pasarguard import get_pg

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
