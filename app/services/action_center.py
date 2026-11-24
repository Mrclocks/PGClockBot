"""Owner and shop work queue: what needs a person now, and the panel link check."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DeliveryFailure,
    Order,
    Payment,
    PaymentStatus,
    Plan,
    Ticket,
    UserService,
)
from app.services.pasarguard import is_pg_permission_denied
from app.services.pg_read_resilience import PG_READ_UNAVAILABLE_MESSAGE, PgReadUnavailable
from app.services.service_scope import shop_services_clause
from app.services.ticket_scope import ticket_shop_clause


async def resolve_pg_open_url(session: AsyncSession, *, is_admin: bool) -> str:
    """External PasarGuard dashboard URL for quick-open button."""
    from app.config import get_settings, normalize_pg_base_url
    from app.services.resellers import get_reseller_pg_panel_base_url

    if is_admin:
        return normalize_pg_base_url(get_settings().pg_base_url or "")
    return await get_reseller_pg_panel_base_url(session)


async def check_pg_connection(
    *,
    reseller_user_id: int | None = None,
    session: AsyncSession | None = None,
    stats_max_age: float | None = None,
) -> dict[str, Any]:
    """Lightweight PasarGuard reachability probe.

    ``stats_max_age`` lets a page reuse system stats it already read (the dashboard overview reads them
    just before); the live ``/home/pg-health`` endpoint leaves it unset.
    """
    try:
        if reseller_user_id and session is not None:
            from app.services.pasarguard import get_pg_for_reseller

            pg = await get_pg_for_reseller(session, int(reseller_user_id))
        else:
            from app.services.pasarguard import get_pg

            pg = get_pg()
        await pg.ensure_token()
        stats = None
        try:
            stats = await pg.get_system_stats(cache_max_age=stats_max_age)
        except Exception as exc:
            if not is_pg_permission_denied(exc):
                raise
            # Limited accounts cannot read server stats; a live self-read still
            # verifies connectivity. A stored token alone does not prove it.
            await pg.get_current_admin()
        version = None
        if isinstance(stats, dict):
            version = stats.get("version")
        return {
            "ok": True,
            "error": None,
            "version": str(version) if version else None,
            "base_url": getattr(pg, "base_url", None),
        }
    except PgReadUnavailable:
        return {
            "ok": None,
            "unchecked": True,
            "error": PG_READ_UNAVAILABLE_MESSAGE,
            "version": None,
            "base_url": None,
        }
    except Exception:
        return {
            "ok": False,
            "error": "اتصال به پاسارگارد برقرار نشد",
            "version": None,
            "base_url": None,
        }


def payment_shop_clause(shop_id: int | None):
    """Payments of one shop (None = platform): a top-up's purse or the order's shop.

    The query must outer-join ``Order`` on ``Order.id == Payment.order_id``.
    """
    if shop_id is None:
        return or_(
            and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id.is_(None)),
            and_(Payment.is_wallet_topup.is_(False), Order.reseller_id.is_(None)),
        )
    return or_(
        and_(Payment.is_wallet_topup.is_(True), Payment.wallet_shop_id == int(shop_id)),
        and_(Payment.is_wallet_topup.is_(False), Order.reseller_id == int(shop_id)),
    )


async def build_action_center(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
    expire_days: int = 3,
) -> dict[str, Any]:
    """Daily ops shortcuts: receipts, tickets, failed deliveries, expiring soon."""
    expire_days = max(1, min(30, int(expire_days or 3)))

    pending_q = (
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            payment_shop_clause(reseller_id),
        )
    )
    tickets_q = select(func.count()).select_from(Ticket).where(
        Ticket.status == "open", ticket_shop_clause(reseller_id)
    )
    fail_q = (
        select(func.count())
        .select_from(DeliveryFailure)
        .where(DeliveryFailure.resolved_at.is_(None))
    )

    if reseller_id is None:
        fail_q = fail_q.where(DeliveryFailure.reseller_id.is_(None))
    else:
        fail_q = fail_q.where(DeliveryFailure.reseller_id == int(reseller_id))

    pending = int((await session.execute(pending_q)).scalar() or 0)
    tickets = int((await session.execute(tickets_q)).scalar() or 0)
    failures = int((await session.execute(fail_q)).scalar() or 0)

    # Expiring: only scan services that could expire in-window (created within
    # max plan length), instead of loading the full service table.
    cutoff = datetime.now(timezone.utc) + timedelta(days=expire_days)
    now = datetime.now(timezone.utc)
    max_days = int(
        (
            await session.execute(
                select(func.coalesce(func.max(Plan.duration_days), 0)).select_from(Plan)
            )
        ).scalar()
        or 0
    )
    expiring = 0
    if max_days > 0:
        scan_since = now - timedelta(days=max_days)
        rows = (
            await session.execute(
                select(UserService.created_at, Plan.duration_days)
                .select_from(UserService)
                .join(Plan, Plan.id == UserService.plan_id)
                .where(
                    UserService.plan_id.is_not(None),
                    Plan.duration_days.is_not(None),
                    UserService.created_at.is_not(None),
                    UserService.created_at >= scan_since,
                    UserService.quota_synced_at.is_(None),
                    UserService.is_cancelled.is_(False),
                    shop_services_clause(reseller_id),
                )
            )
        ).all()
        for created, days in rows:
            if not created or not days:
                continue
            try:
                exp = created
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                exp = exp + timedelta(days=int(days))
            except (TypeError, ValueError, OverflowError):
                # A plan with an unusable duration is left out of the count, not the whole list.
                continue
            if now <= exp <= cutoff:
                expiring += 1

    synced_q = (
        select(func.count())
        .select_from(UserService)
        .where(
            UserService.quota_synced_at.is_not(None),
            UserService.quota_expire_at.is_not(None),
            UserService.quota_expire_at >= now,
            UserService.quota_expire_at <= cutoff,
            UserService.is_cancelled.is_(False),
            shop_services_clause(reseller_id),
        )
    )
    expiring += int((await session.execute(synced_q)).scalar() or 0)

    items: list[dict[str, Any]] = []
    if pending:
        items.append(
            {
                "key": "pending",
                "title": f"{pending} رسید در انتظار",
                "detail": "تأیید یا رد پرداخت‌ها",
                "href": "/finance?tab=payments",
                "tone": "warn",
            }
        )
    if failures:
        items.append(
            {
                "key": "delivery",
                "title": f"{failures} تحویل ناموفق",
                "detail": "صف تلاش مجدد",
                "href": "/finance?tab=delivery",
                "tone": "err",
            }
        )
    if tickets:
        items.append(
            {
                "key": "tickets",
                "title": f"{tickets} تیکت باز",
                "detail": "پشتیبانی کاربران",
                "href": "/tickets",
                "tone": "warn",
            }
        )
    if expiring:
        from app.services.users_ops import users_list_href

        items.append(
            {
                "key": "expiring",
                "title": f"{expiring} سرویس نزدیک انقضا",
                "detail": f"تا {expire_days} روز آینده",
                "href": users_list_href(filter_key="expiring"),
                "tone": "neutral",
            }
        )

    if reseller_id is None:
        from app.services.service_shop_review import count_services_under_review

        waiting = await count_services_under_review(session)
        if waiting:
            items.append(
                {
                    "key": "shop_review",
                    "title": f"{waiting} سرویس در انتظار تعیین فروشگاه",
                    "detail": "تا تعیین نشود برای هیچ فروشگاهی نمایش داده نمی‌شود",
                    "href": "/service-shop-review",
                    "tone": "warn",
                }
            )

    # Low volume: services still flagged by the alert scheduler (clears on renew).
    low_q = (
        select(func.count())
        .select_from(UserService)
        .where(
            UserService.notified_traffic.is_(True),
            UserService.is_cancelled.is_(False),
            shop_services_clause(reseller_id),
        )
    )
    low_volume = int((await session.execute(low_q)).scalar() or 0)
    if low_volume:
        from app.services.users_ops import users_list_href

        items.append(
            {
                "key": "low_volume",
                "title": f"{low_volume} سرویس با حجم کم",
                "detail": "اعلان حجم فعال",
                "href": users_list_href(filter_key="low_volume"),
                "tone": "warn",
            }
        )

    return {
        "pending": pending,
        "tickets": tickets,
        "failures": failures,
        "expiring": expiring,
        "low_volume": low_volume,
        # Key must NOT be named ``items`` — Jinja ``ac.items`` resolves to dict.items().
        "entries": items,
        "has_items": bool(items),
    }
