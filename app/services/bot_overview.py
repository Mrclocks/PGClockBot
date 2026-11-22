"""Figures of the bot overview (/dashboard): period KPIs against the period before, bot status.

Every query is scoped to one shop (``reseller_id`` None = the platform's own shop) and leaves
out demo customers, like the home dashboard. The bot status probe is cached briefly per bot so
opening the page never waits on Telegram more than once a minute.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment, Plan, ResellerProfile
from app.services.authz import authz_from_staff, can_shop
from app.services.db_safe import rollback_quiet
from app.services.demo_users import non_demo_customer
from app.services.home_overview import bot_panel_summary, check_bot_connection, reseller_shop_summary
from app.services.resellers import bot_needs_setup
from app.services.secret_box import reveal_bot_token
from app.services.setup_wizard import current_setup_values
from app.services.shop_membership import shop_member_clause
from app.services.shop_scope import empty_shop_stats, is_platform_admin, shop_owner_id
from app.services.action_center import payment_shop_clause

_TEHRAN = ZoneInfo("Asia/Tehran")

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
DEFAULT_PERIOD = "week"
PERIOD_LABELS = {"day": "امروز", "week": "۷ روز", "month": "۳۰ روز"}
COMPARE_LABELS = {"day": "نسبت به دیروز", "week": "نسبت به ۷ روز قبل", "month": "نسبت به ۳۰ روز قبل"}

BOT_STATUS_TTL_SECONDS = 60.0
BOT_STATUS_TIMEOUT_SECONDS = 2.5
BOT_STATUS_CACHE_SIZE = 512
RECENT_ROWS = 6


@dataclass(frozen=True, slots=True)
class PeriodFigures:
    revenue: int
    delivered: int
    new_users: int


@dataclass(frozen=True, slots=True)
class PeriodKpis:
    period: str
    current: PeriodFigures
    previous: PeriodFigures

    def change_pct(self, field: str) -> int | None:
        return change_pct(getattr(self.current, field), getattr(self.previous, field))


def parse_period(raw: str | None) -> str:
    """A known period name; anything else falls back to the default."""
    value = (raw or "").strip().lower()
    return value if value in PERIOD_DAYS else DEFAULT_PERIOD


def change_pct(current: int, previous: int) -> int | None:
    """Whole-percent change, or None when there is nothing to compare against."""
    if previous <= 0:
        return None
    return round((current - previous) * 100 / previous)


def period_windows(period: str, *, now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """(previous start, current start, end) in UTC, on Asia/Tehran day boundaries."""
    days = PERIOD_DAYS[parse_period(period)]
    local = (now or datetime.now(timezone.utc)).astimezone(_TEHRAN)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    current_start = today - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    end = today + timedelta(days=1)
    return (
        previous_start.astimezone(timezone.utc),
        current_start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
    )


async def period_kpis(
    session: AsyncSession, *, reseller_id: int | None, period: str, now: datetime | None = None
) -> PeriodKpis:
    """Revenue, delivered orders and new customers of one shop, this period and the one before."""
    period = parse_period(period)
    prev_start, cur_start, end = period_windows(period, now=now)
    orders_scope = Order.reseller_id.is_(None) if reseller_id is None else Order.reseller_id == int(reseller_id)
    delivered = Order.status == "delivered"
    in_current = Order.created_at >= cur_start

    def _sum(when, value):
        return func.coalesce(func.sum(case((when, value), else_=0)), 0)

    orders = (
        await session.execute(
            select(
                _sum(delivered & in_current, Order.amount),
                _sum(delivered & ~in_current, Order.amount),
                _sum(delivered & in_current, 1),
                _sum(delivered & ~in_current, 1),
            ).where(
                Order.created_at >= prev_start,
                Order.created_at < end,
                orders_scope,
                non_demo_customer(Order.user_id),
            )
        )
    ).one()
    user_current = BotUser.created_at >= cur_start
    users = (
        await session.execute(
            select(_sum(user_current, 1), _sum(~user_current, 1)).where(
                BotUser.created_at >= prev_start,
                BotUser.created_at < end,
                shop_member_clause(reseller_id),
                BotUser.is_demo.is_(False),
            )
        )
    ).one()
    return PeriodKpis(
        period=period,
        current=PeriodFigures(revenue=int(orders[0]), delivered=int(orders[2]), new_users=int(users[0])),
        previous=PeriodFigures(revenue=int(orders[1]), delivered=int(orders[3]), new_users=int(users[1])),
    )


_bot_status_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def clear_bot_status_cache() -> None:
    """Forget every cached bot probe (the «پاک کردن کش‌ها» tool); the next page asks Telegram again."""
    _bot_status_cache.clear()


def _cache_key(token: str) -> str:
    # The token itself never becomes a dict key that could end up in a dump or log.
    return hashlib.sha256(token.encode()).hexdigest()


async def bot_status(token: str | None) -> dict[str, Any]:
    """``{"state": ok|err|neutral, "username"}`` for a bot, from a short per-bot cache."""
    token = (token or "").strip()
    if not token:
        return {"state": "err", "username": None, "detail": "توکن تنظیم نشده"}
    key = _cache_key(token)
    hit = _bot_status_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < BOT_STATUS_TTL_SECONDS:
        return hit[1]
    try:
        probe = await asyncio.wait_for(check_bot_connection(token), timeout=BOT_STATUS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {"state": "neutral", "username": None, "detail": "بررسی نشد"}
    status = {
        "state": "ok" if probe.get("ok") else "err",
        "username": probe.get("username"),
        "detail": "آنلاین" if probe.get("ok") else "قطع",
    }
    _bot_status_cache[key] = (now, status)
    _bot_status_cache.move_to_end(key)
    while len(_bot_status_cache) > BOT_STATUS_CACHE_SIZE:
        _bot_status_cache.popitem(last=False)
    return status


@dataclass(frozen=True, slots=True)
class Viewer:
    """Whose figures the page shows and which of them this staff member may open."""

    shop_id: int | None
    has_shop: bool
    can_payments: bool
    can_orders: bool
    can_tickets: bool
    can_settings: bool


def viewer_of(staff: dict) -> Viewer:
    """Platform owner: the platform shop with every link. Shop staff: their shop, by permission.

    Anyone else (a bare admin session without the owner principal) gets no shop: fail closed.
    """
    if is_platform_admin(staff):
        return Viewer(
            shop_id=None, has_shop=True, can_payments=True, can_orders=True, can_tickets=True, can_settings=True
        )
    rid = shop_owner_id(staff)
    if not rid:
        return Viewer(
            shop_id=None, has_shop=False, can_payments=False, can_orders=False, can_tickets=False, can_settings=False
        )
    ctx = authz_from_staff(staff)
    return Viewer(
        shop_id=int(rid),
        has_shop=True,
        can_payments=can_shop(ctx, "payments"),
        can_orders=can_shop(ctx, "orders"),
        can_tickets=can_shop(ctx, "tickets"),
        can_settings=can_shop(ctx, "shop_settings"),
    )


async def shop_totals(session: AsyncSession, viewer: Viewer) -> dict[str, int]:
    """All-time totals of the viewer's shop, as the home dashboard counts them."""
    if not viewer.has_shop:
        return empty_shop_stats()
    try:
        if viewer.shop_id is not None:
            return await reseller_shop_summary(session, viewer.shop_id)
        summary = await bot_panel_summary(session)
        plans = await session.scalar(
            select(func.count()).select_from(Plan).where(Plan.is_active.is_(True), Plan.owner_reseller_id.is_(None))
        )
    except Exception:
        # Decorative figures: a failed aggregate shows zeros, never a broken page.
        await rollback_quiet(session)
        return empty_shop_stats()
    return {**summary, "plans": int(plans or 0)}


async def recent_rows(session: AsyncSession, viewer: Viewer) -> tuple[list[Payment], list[Order]]:
    """The latest payments and orders of the viewer's shop, each only when they may see it."""
    payments: list[Payment] = []
    orders: list[Order] = []
    if viewer.can_payments:
        payments = list(
            (
                await session.execute(
                    select(Payment)
                    .outerjoin(Order, Order.id == Payment.order_id)
                    .where(payment_shop_clause(viewer.shop_id))
                    .order_by(Payment.id.desc())
                    .limit(RECENT_ROWS)
                )
            ).scalars()
        )
    if viewer.can_orders:
        scope = Order.reseller_id.is_(None) if viewer.shop_id is None else Order.reseller_id == viewer.shop_id
        orders = list(
            (await session.execute(select(Order).where(scope).order_by(Order.id.desc()).limit(RECENT_ROWS))).scalars()
        )
    return payments, orders


async def shop_bot_token(session: AsyncSession, viewer: Viewer) -> str | None:
    if viewer.shop_id is None:
        return current_setup_values().get("BOT_TOKEN")
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == viewer.shop_id))
    return reveal_bot_token(profile.bot_token) if profile else None


async def shop_needs_bot(session: AsyncSession, viewer: Viewer) -> bool:
    """A shop without its own bot sees only the setup notice."""
    if viewer.shop_id is None:
        return False
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == viewer.shop_id))
    return bot_needs_setup(profile)
