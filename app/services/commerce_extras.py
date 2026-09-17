"""Commerce extras: auto-renew, cart recovery, pause, predictive traffic, coaching.

All helpers are fail-closed on ownership. Platform Mini App never sets shop
ContextVar — catalog/order helpers stay on platform plans unless explicitly
passed a verified shop scope (not used here).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    FunnelEvent,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    ResellerProfile,
    Ticket,
    TicketStatus,
    UserService,
)
from app.services.users import get_all_settings, on

log = logging.getLogger(__name__)


def _as_int(val: Any, default: int) -> int:
    try:
        return int(float(val))
    except Exception:
        return default


def onboarding_steps(*, subscription_url: str = "") -> list[dict[str, str]]:
    """Post-purchase install kit — short steps shown in Mini App."""
    steps = [
        {
            "title": "لینک اشتراک",
            "body": "لینک سرویس را کپی کنید یا QR را اسکن کنید.",
        },
        {
            "title": "نصب کلاینت",
            "body": "اپلیکیشن سازگار با لینک اشتراک را روی دستگاه نصب کنید.",
        },
        {
            "title": "افزودن اشتراک",
            "body": "لینک را در کلاینت وارد کنید و اتصال را تست کنید.",
        },
        {
            "title": "تمدید به‌موقع",
            "body": "قبل از اتمام حجم یا زمان، از تب سرویس تمدید کنید.",
        },
    ]
    if subscription_url:
        steps[0]["body"] = "لینک آماده است — از دکمه کپی یا QR استفاده کنید."
    return steps


async def diagnose_user_services(
    session: AsyncSession, user: BotUser, *, services: list[UserService] | None = None
) -> dict[str, Any]:
    """Smart support pre-check — own services only, allowlisted tips."""
    from app.api.miniapp_pages import _fetch_pg_info

    rows = services
    if rows is None:
        rows = list(
            (
                await session.execute(
                    select(UserService)
                    .where(UserService.bot_user_id == int(user.id))
                    .order_by(UserService.id.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
    tips: list[str] = []
    findings: list[dict[str, Any]] = []
    for svc in rows:
        if int(svc.bot_user_id) != int(user.id):
            continue
        info = await _fetch_pg_info(svc.subscription_token)
        status = (info.get("status") or "").lower()
        used = float(info.get("used_traffic") or 0)
        limit = float(info.get("data_limit") or 0)
        expire = info.get("expire")
        tip = None
        if svc.paused_at is not None:
            tip = "سرویس موقتاً متوقف است — از مینی‌اپ از سر بگیرید."
        elif status in {"disabled", "limited", "expired"}:
            tip = "وضعیت سرویس محدود/منقضی است — تمدید یا خرید جدید لازم است."
        elif limit > 0 and used >= limit:
            tip = "حجم تمام شده — تمدید یا پلن حجیم‌تر پیشنهاد می‌شود."
        elif expire:
            try:
                from app.services.formatting import parse_expire

                exp = parse_expire(expire)
                if exp and exp <= datetime.now(timezone.utc):
                    tip = "زمان سرویس تمام شده — تمدید کنید."
            except Exception:
                pass
        if tip:
            tips.append(f"{svc.pg_username or 'سرویس'}: {tip}")
        findings.append(
            {
                "service_id": svc.id,
                "username": svc.pg_username or "",
                "tip": tip,
                "paused": svc.paused_at is not None,
            }
        )
    if not rows:
        tips.append("سرویسی ندارید — از بخش خرید شروع کنید.")
    return {"tips": tips[:8], "findings": findings, "ok": not tips}


def suggest_renew_plan(
    plans: list[Plan],
    *,
    traffic_pct: int | None,
    expire_days: int | None,
) -> Plan | None:
    """Pick a personalized renew suggestion from platform catalog."""
    active = [p for p in plans if p and p.is_active and not p.is_trial]
    if not active:
        return None
    # Heavy traffic users → prefer higher GB; time-short → longer duration.
    scored: list[tuple[float, Plan]] = []
    for p in active:
        gb = float(p.data_limit_gb or 0) or 9999.0
        days = float(p.duration_days or 30)
        score = 0.0
        if traffic_pct is not None and traffic_pct >= 70:
            score += gb
        if expire_days is not None and expire_days <= 5:
            score += days
        if score == 0:
            score = 1.0 / max(1.0, float(p.price or 1))
        scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], int(x[1].price or 0)))
    return scored[0][1]


async def set_auto_renew(
    session: AsyncSession,
    *,
    user: BotUser,
    service: UserService,
    enabled: bool,
    plan_id: int | None = None,
) -> UserService:
    if int(service.bot_user_id) != int(user.id):
        raise ValueError("سرویس متعلق به شما نیست")
    ui = await get_all_settings(session)
    if enabled and not on(ui.get("auto_renew_enabled", "0")):
        raise ValueError("تمدید خودکار در تنظیمات فروشگاه غیرفعال است")
    if enabled:
        if not plan_id:
            plan_id = service.plan_id or service.auto_renew_plan_id
        if not plan_id:
            raise ValueError("پلن تمدید را انتخاب کنید")
        plan = await session.get(Plan, int(plan_id))
        if not plan or not plan.is_active or plan.is_trial or plan.owner_reseller_id is not None:
            raise ValueError("پلن تمدید نامعتبر است")
        service.auto_renew_enabled = True
        service.auto_renew_plan_id = int(plan.id)
        service.auto_renew_fail_count = 0
    else:
        service.auto_renew_enabled = False
    await session.commit()
    await session.refresh(service)
    return service


async def pause_service(
    session: AsyncSession,
    *,
    user: BotUser,
    service: UserService,
    pause: bool,
    reason: str = "",
) -> UserService:
    if int(service.bot_user_id) != int(user.id):
        raise ValueError("سرویس متعلق به شما نیست")
    ui = await get_all_settings(session)
    if not on(ui.get("user_self_pause_enabled", "1")):
        raise ValueError("توقف سرویس غیرفعال است")
    if not service.pg_user_id:
        raise ValueError("سرویس به پنل وصل نیست")
    from app.services.pasarguard import get_pg

    await get_pg().set_disabled_by_id(int(service.pg_user_id), bool(pause))
    if pause:
        service.paused_at = datetime.now(timezone.utc)
        service.pause_reason = (reason or "user")[:255]
    else:
        service.paused_at = None
        service.pause_reason = None
    await session.commit()
    await session.refresh(service)
    return service


async def grant_emergency_credit(
    session: AsyncSession, *, user: BotUser
) -> dict[str, Any]:
    """Small wallet credit for eligible users; debt repaid on next paid order."""
    from sqlalchemy import update

    ui = await get_all_settings(session)
    if not on(ui.get("emergency_credit_enabled", "0")):
        raise ValueError("اعتبار اضطراری غیرفعال است")
    max_amt = max(0, _as_int(ui.get("emergency_credit_max_toman"), 20000))
    if max_amt <= 0:
        raise ValueError("سقف اعتبار تنظیم نشده")
    # Eligibility: delivered order history + low/zero wallet
    delivered = (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.user_id == int(user.id),
                Order.status == OrderStatus.DELIVERED.value,
            )
        )
    ).scalar_one()
    if int(delivered or 0) < 1:
        raise ValueError("فقط مشتریان با سابقه خرید مجازند")
    if int(user.wallet_balance or 0) >= max_amt:
        raise ValueError("موجودی کافی است؛ نیازی به اعتبار اضطراری نیست")

    # Atomic debt claim — only one concurrent grant can win.
    with session.no_autoflush:
        claim = await session.execute(
            update(BotUser)
            .where(
                BotUser.id == int(user.id),
                BotUser.emergency_credit_debt == 0,
            )
            .values(emergency_credit_debt=int(max_amt))
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("اعتبار قبلی هنوز تسویه نشده")
    await session.refresh(user)

    from app.services.wallet import credit_wallet

    try:
        await credit_wallet(
            session,
            user,
            max_amt,
            reason=f"emergency_credit:{max_amt}",
            commit=False,
        )
        await session.commit()
        await session.refresh(user)
    except Exception:
        # Roll debt claim back so the user can retry after a credit failure.
        user.emergency_credit_debt = 0
        await session.commit()
        raise
    return {"credited": max_amt, "debt": int(user.emergency_credit_debt or 0)}


async def repay_emergency_credit_if_needed(
    session: AsyncSession, *, user: BotUser, order: Order | None = None
) -> int:
    """Debit outstanding debt after a successful paid order (best-effort)."""
    from sqlalchemy import update

    debt = int(getattr(user, "emergency_credit_debt", 0) or 0)
    if debt <= 0:
        return 0
    if order is not None and int(order.user_id) != int(user.id):
        return 0
    bal = int(user.wallet_balance or 0)
    if bal <= 0:
        return 0
    pay = min(debt, bal)
    if pay <= 0:
        return 0
    from app.services.wallet import debit_wallet

    reason = (
        f"emergency_credit_repay:{order.id}"
        if order is not None
        else "emergency_credit_repay"
    )
    try:
        await debit_wallet(session, user, pay, reason=reason, commit=False)
        with session.no_autoflush:
            await session.execute(
                update(BotUser)
                .where(
                    BotUser.id == int(user.id),
                    BotUser.emergency_credit_debt >= int(pay),
                )
                .values(emergency_credit_debt=BotUser.emergency_credit_debt - int(pay))
                .execution_options(synchronize_session=False)
            )
        await session.commit()
        await session.refresh(user)
    except Exception:
        await session.rollback()
        return 0
    return pay


async def health_check_service(
    session: AsyncSession, *, user: BotUser, service: UserService
) -> dict[str, Any]:
    if int(service.bot_user_id) != int(user.id):
        raise ValueError("سرویس متعلق به شما نیست")
    from app.api.miniapp_pages import _fetch_pg_info, _serialize_service

    info = await _fetch_pg_info(service.subscription_token)
    ser = _serialize_service(service, info)
    tips = []
    if service.paused_at:
        tips.append("سرویس متوقف است.")
    if ser.get("error") == "upstream_unavailable":
        tips.append("وضعیت لحظه‌ای در دسترس نیست؛ بعداً دوباره تلاش کنید.")
    elif (ser.get("status") or "").lower() in {"disabled", "expired", "limited"}:
        tips.append("وضعیت سرویس نیاز به تمدید دارد.")
    pct = ser.get("traffic_pct")
    if pct is not None and pct >= 90:
        tips.append("حجم رو به اتمام است.")
    days = ser.get("expire_days")
    if days is not None and days <= 2:
        tips.append("کمتر از ۲ روز تا انقضا مانده.")
    return {
        "service": ser,
        "healthy": not tips and ser.get("error") is None,
        "tips": tips,
    }


async def update_traffic_prediction(
    session: AsyncSession, service: UserService, *, used_bytes: float, limit_bytes: float
) -> None:
    """Update ETA from last sample; skip if unlimited / no progress."""
    now = datetime.now(timezone.utc)
    prev_used = service.quota_used_bytes
    prev_at = service.traffic_sample_at
    service.quota_used_bytes = int(used_bytes) if used_bytes is not None else None
    service.traffic_sample_at = now
    if (
        prev_used is None
        or prev_at is None
        or limit_bytes <= 0
        or used_bytes <= float(prev_used)
    ):
        await session.flush()
        return
    delta_bytes = float(used_bytes) - float(prev_used)
    delta_sec = max(1.0, (now - prev_at).total_seconds())
    rate = delta_bytes / delta_sec
    if rate <= 0:
        service.predicted_exhaust_at = None
    else:
        remain = max(0.0, float(limit_bytes) - float(used_bytes))
        eta_sec = remain / rate
        service.predicted_exhaust_at = now + timedelta(seconds=eta_sec)
    await session.flush()


async def reseller_coaching_stats(
    session: AsyncSession, *, profile: ResellerProfile
) -> dict[str, Any]:
    """Actionable shop metrics for the owning reseller only."""
    rid = int(profile.user_id)
    pending = (
        await session.execute(
            select(func.count())
            .select_from(Payment)
            .join(Order, Payment.order_id == Order.id)
            .where(
                Payment.status == PaymentStatus.PENDING.value,
                Payment.receipt_file_id.is_not(None),
                Order.reseller_id == rid,
            )
        )
    ).scalar_one()
    open_tickets = (
        await session.execute(
            select(func.count())
            .select_from(Ticket)
            .where(
                Ticket.reseller_id == rid,
                Ticket.status == TicketStatus.OPEN.value,
            )
        )
    ).scalar_one()
    delivered = (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.reseller_id == rid,
                Order.status == OrderStatus.DELIVERED.value,
            )
        )
    ).scalar_one()
    pay_starts = (
        await session.execute(
            select(func.count())
            .select_from(FunnelEvent)
            .where(
                FunnelEvent.reseller_id == rid,
                FunnelEvent.step == "pay_start",
            )
        )
    ).scalar_one()
    tips: list[str] = []
    if int(pending or 0) > 0:
        tips.append(f"{int(pending)} رسید در انتظار تأیید — اول صف مالی را خالی کنید.")
    if int(open_tickets or 0) > 0:
        tips.append(f"{int(open_tickets)} تیکت باز — پاسخ سریع رضایت را بالا می‌برد.")
    if int(pay_starts or 0) > 5 and int(delivered or 0) * 3 < int(pay_starts or 0):
        tips.append("ریزش بین شروع پرداخت و تحویل زیاد است — متن کارت/درگاه را بررسی کنید.")
    if not tips:
        tips.append("وضعیت پایدار است — تمدید مشتریان نزدیک به انقضا را پیگیری کنید.")
    return {
        "pending_receipts": int(pending or 0),
        "open_tickets": int(open_tickets or 0),
        "delivered_orders": int(delivered or 0),
        "funnel_pay_starts": int(pay_starts or 0),
        "tips": tips[:5],
    }


async def run_auto_renew_tick(session: AsyncSession, bot) -> dict[str, int]:
    """Attempt wallet auto-renew for eligible services (platform catalog)."""
    from app.services.orders import (
        get_catalog_plan,
        pay_with_wallet,
        renew_service_with_plan,
    )

    ui = await get_all_settings(session)
    if not on(ui.get("auto_renew_enabled", "0")):
        return {"skipped": 1}
    days_before = max(0, min(14, _as_int(ui.get("auto_renew_days_before"), 1)))
    now = datetime.now(timezone.utc)
    rows = list(
        (
            await session.execute(
                select(UserService)
                .where(UserService.auto_renew_enabled.is_(True))
                .order_by(UserService.id.asc())
                .limit(80)
            )
        )
        .scalars()
        .all()
    )
    ok = fail = 0
    for svc in rows:
        if svc.paused_at is not None:
            continue
        user = await session.get(BotUser, svc.bot_user_id)
        if not user or user.is_blocked:
            continue
        plan_id = svc.auto_renew_plan_id or svc.plan_id
        if not plan_id:
            continue
        # Cheap gate: cached expire when present
        if svc.quota_expire_at is not None:
            if svc.quota_expire_at > now + timedelta(days=days_before):
                continue
        try:
            plan = await get_catalog_plan(session, int(plan_id))
            if not plan or plan.owner_reseller_id is not None or plan.is_trial:
                raise ValueError("bad plan")
            if int(user.wallet_balance or 0) < int(plan.price or 0):
                raise ValueError("low wallet")
            order = await renew_service_with_plan(
                session, user_id=user.id, service=svc, plan=plan
            )
            await session.refresh(user)
            await pay_with_wallet(session, order, user)
            svc.auto_renew_fail_count = 0
            svc.auto_renew_last_at = now
            svc.notified_expire = False
            ok += 1
            try:
                await bot.send_message(
                    int(user.telegram_id),
                    f"✅ تمدید خودکار سرویس {svc.pg_username} انجام شد.",
                )
            except Exception:
                pass
        except Exception:
            svc.auto_renew_fail_count = int(svc.auto_renew_fail_count or 0) + 1
            fail += 1
            if svc.auto_renew_fail_count >= 3:
                svc.auto_renew_enabled = False
            try:
                await bot.send_message(
                    int(user.telegram_id),
                    f"⚠️ تمدید خودکار {svc.pg_username} ناموفق بود. کیف پول را شارژ کنید.",
                )
            except Exception:
                pass
        await session.commit()
    return {"ok": ok, "fail": fail}


async def run_cart_recovery_tick(session: AsyncSession, bot) -> dict[str, int]:
    ui = await get_all_settings(session)
    if not on(ui.get("cart_recovery_enabled", "0")):
        return {"skipped": 1}
    after_h = max(1, min(72, _as_int(ui.get("cart_recovery_after_hours"), 2)))
    max_sends = max(1, min(3, _as_int(ui.get("cart_recovery_max_sends"), 2)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=after_h)
    statuses = (
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.REJECTED.value,
    )
    rows = list(
        (
            await session.execute(
                select(Order)
                .where(
                    Order.status.in_(statuses),
                    Order.created_at <= cutoff,
                    Order.cart_remind_count < max_sends,
                    or_(
                        Order.cart_reminded_at.is_(None),
                        Order.cart_reminded_at
                        <= datetime.now(timezone.utc) - timedelta(hours=after_h),
                    ),
                )
                .order_by(Order.id.asc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for order in rows:
        # Only platform-shop abandoned carts in this tick (Mini App / main bot).
        if order.reseller_id is not None:
            continue
        user = await session.get(BotUser, order.user_id)
        if not user or user.is_blocked:
            continue
        try:
            await bot.send_message(
                int(user.telegram_id),
                (
                    f"🛒 سفارش #{order.id} هنوز پرداخت نشده.\n"
                    f"مبلغ: {int(order.amount or 0):,} تومان\n"
                    "از مینی‌اپ یا ربات پرداخت را کامل کنید."
                ).replace(",", "٬"),
            )
            order.cart_remind_count = int(order.cart_remind_count or 0) + 1
            order.cart_reminded_at = datetime.now(timezone.utc)
            sent += 1
            await session.commit()
        except Exception:
            await session.rollback()
    return {"sent": sent}


async def list_segment_targets(
    session: AsyncSession,
    *,
    segment: str,
    reseller_id: int | None = None,
    limit: int = 500,
) -> list[BotUser]:
    """Broadcast-style audiences beyond role filters (platform or shop-scoped)."""
    segment = (segment or "").strip()
    lim = max(1, min(2000, int(limit)))
    now = datetime.now(timezone.utc)
    if segment == "expiring_soon":
        q = (
            select(BotUser)
            .join(UserService, UserService.bot_user_id == BotUser.id)
            .where(
                BotUser.is_blocked.is_(False),
                UserService.quota_expire_at.is_not(None),
                UserService.quota_expire_at <= now + timedelta(days=3),
                UserService.quota_expire_at >= now,
            )
        )
    elif segment == "abandoned_cart":
        q = (
            select(BotUser)
            .join(Order, Order.user_id == BotUser.id)
            .where(
                BotUser.is_blocked.is_(False),
                Order.status.in_(
                    (
                        OrderStatus.PENDING.value,
                        OrderStatus.AWAITING_RECEIPT.value,
                    )
                ),
            )
        )
    elif segment == "low_traffic":
        q = (
            select(BotUser)
            .join(UserService, UserService.bot_user_id == BotUser.id)
            .where(
                BotUser.is_blocked.is_(False),
                UserService.notified_traffic.is_(True),
            )
        )
    else:
        return []
    if reseller_id is None:
        q = q.where(BotUser.reseller_id.is_(None))
        if segment == "abandoned_cart":
            q = q.where(Order.reseller_id.is_(None))
    else:
        rid = int(reseller_id)
        q = q.where(BotUser.reseller_id == rid)
        if segment == "abandoned_cart":
            q = q.where(Order.reseller_id == rid)
    q = q.distinct().limit(lim)
    return list((await session.execute(q)).scalars().all())
