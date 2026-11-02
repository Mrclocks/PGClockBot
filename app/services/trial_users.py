"""Customers who took the free trial, and whether they went on to buy — one shop at a time.

A shop (``None`` = the platform) only ever sees trials delivered by its own bot. A buyer is
someone with a delivered, paid, non-trial order in that same shop; renewals count, a
reseller agency fee does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import BotUser, MarketingPreference, Order, Plan, UserService
from app.services.formatting import status_label_plain
from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

PAGE_SIZE = 50
MAX_SEARCH_LENGTH = 64
RESELLER_APP_NOTE_PREFIX = "reseller_app:"

TrialFilter = Literal["all", "buyers", "non_buyers"]
FILTER_LABELS: dict[str, str] = {
    "all": "همه",
    "non_buyers": "بدون خرید",
    "buyers": "خریدار",
}
DEFAULT_FILTER: TrialFilter = "all"

TrialState = Literal["active", "ended", "unknown"]
_ENDED_STATUSES = frozenset({"expired", "limited", "disabled"})


@dataclass(slots=True)
class TrialUserRow:
    """One customer's trial, as the marketing table shows it."""

    user_id: int
    name: str
    telegram_id: int
    trial_order_id: int
    trial_at: datetime | None
    trial_state: TrialState
    trial_state_label: str
    trial_ends_at: datetime | None
    is_buyer: bool
    purchase_count: int
    first_purchase_at: datetime | None
    total_spent: int
    is_blocked: bool
    is_opted_out: bool
    can_manage: bool


@dataclass(slots=True)
class TrialUsersPage:
    rows: list[TrialUserRow]
    next_before: int | None


@dataclass(slots=True)
class TrialSummary:
    trials: int
    buyers: int

    @property
    def conversion_percent(self) -> int:
        return round(self.buyers * 100 / self.trials) if self.trials else 0


def normalize_filter(raw: str | None) -> TrialFilter:
    key = str(raw or "").strip()
    if key == "buyers" or key == "non_buyers":
        return key
    return DEFAULT_FILTER


def _shop_order_clause(order: type[Order], shop_id: int | None) -> ColumnElement[bool]:
    return order.reseller_id.is_(None) if shop_id is None else order.reseller_id == int(shop_id)


def _purchase_clause(order: type[Order], shop_id: int | None) -> ColumnElement[bool]:
    """A delivered paid order of this shop that is not a trial and not an agency fee."""
    trial_plan = exists(select(Plan.id).where(Plan.id == order.plan_id, Plan.is_trial.is_(True)))
    return and_(
        _shop_order_clause(order, shop_id),
        order.status == "delivered",
        order.amount > 0,
        or_(order.note.is_(None), ~order.note.like(f"{RESELLER_APP_NOTE_PREFIX}%")),
        ~trial_plan,
    )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def trial_orders_subquery(shop_id: int | None):
    """The first delivered trial order of every customer of this shop."""
    return (
        select(Order.user_id.label("user_id"), func.min(Order.id).label("order_id"))
        .join(Plan, Plan.id == Order.plan_id)
        .where(_shop_order_clause(Order, shop_id), Order.status == "delivered", Plan.is_trial.is_(True))
        .group_by(Order.user_id)
        .subquery()
    )


def _is_buyer_clause(shop_id: int | None, user_column) -> ColumnElement[bool]:
    purchase = aliased(Order)
    return exists(select(purchase.id).where(purchase.user_id == user_column, _purchase_clause(purchase, shop_id)))


def _as_utc(value: datetime | None) -> datetime | None:
    # SQLite returns naive values for timezone-aware columns.
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def trial_state(service: UserService | None, *, now: datetime) -> tuple[TrialState, str]:
    """Where the trial service stands, from the locally cached quota (never the panel)."""
    if service is None or service.quota_synced_at is None:
        return "unknown", "نامشخص"
    status = (service.quota_status or "").strip().lower()
    expires = _as_utc(service.quota_expire_at)
    if status in _ENDED_STATUSES or (expires is not None and expires <= now):
        return "ended", status_label_plain(status) if status in _ENDED_STATUSES else "منقضی"
    return "active", status_label_plain(status) if status else "فعال"


async def trial_summary(session: AsyncSession, *, shop_id: int | None) -> TrialSummary:
    """Trial takers and how many of them bought, in two aggregate queries."""
    trials = trial_orders_subquery(shop_id)
    total = await session.scalar(select(func.count()).select_from(trials)) or 0
    buyers = (
        await session.scalar(
            select(func.count()).select_from(trials).where(_is_buyer_clause(shop_id, trials.c.user_id))
        )
        or 0
    )
    return TrialSummary(trials=int(total), buyers=int(buyers))


async def _purchase_stats(
    session: AsyncSession, user_ids: list[int], *, shop_id: int | None
) -> dict[int, tuple[int, datetime | None, int]]:
    """(count, first purchase time, total spent) per customer, for one page in one query."""
    if not user_ids:
        return {}
    purchase = aliased(Order)
    result = await session.execute(
        select(
            purchase.user_id,
            func.count(purchase.id),
            func.min(purchase.created_at),
            func.coalesce(func.sum(purchase.amount), 0),
        )
        .where(purchase.user_id.in_(user_ids), _purchase_clause(purchase, shop_id))
        .group_by(purchase.user_id)
    )
    return {int(uid): (int(count), first, int(spent)) for uid, count, first, spent in result.all()}


async def _opted_out_ids(session: AsyncSession, user_ids: list[int], *, shop_id: int | None) -> set[int]:
    if not user_ids:
        return set()
    rows = await session.scalars(
        select(MarketingPreference.user_id).where(
            MarketingPreference.user_id.in_(user_ids),
            MarketingPreference.shop_key == (shop_id or 0),
            MarketingPreference.enabled.is_(False),
        )
    )
    return {int(uid) for uid in rows.all()}


def _can_manage(staff: dict, user: BotUser) -> bool:
    """Block and message act on the whole person, so only a customer the shop owns qualifies."""
    try:
        assert_bot_user_in_scope(staff, user)
    except ShopScopeError:
        return False
    return True


async def list_trial_users(
    session: AsyncSession,
    *,
    staff: dict,
    shop_id: int | None,
    trial_filter: TrialFilter = DEFAULT_FILTER,
    search: str = "",
    before: int | None = None,
    limit: int = PAGE_SIZE,
    now: datetime | None = None,
) -> TrialUsersPage:
    """One newest-first page of trial takers; ``before`` is the last trial order id seen."""
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), PAGE_SIZE))
    trials = trial_orders_subquery(shop_id)
    query = (
        select(Order, BotUser, UserService)
        .select_from(trials)
        .join(Order, Order.id == trials.c.order_id)
        .join(BotUser, BotUser.id == trials.c.user_id)
        .outerjoin(UserService, UserService.id == Order.service_id)
    )
    if trial_filter == "buyers":
        query = query.where(_is_buyer_clause(shop_id, BotUser.id))
    elif trial_filter == "non_buyers":
        query = query.where(~_is_buyer_clause(shop_id, BotUser.id))
    term = search.strip()[:MAX_SEARCH_LENGTH]
    if term:
        pattern = f"%{_escape_like(term.lower().lstrip('@'))}%"
        query = query.where(
            or_(
                func.lower(func.coalesce(BotUser.full_name, "")).like(pattern, escape="\\"),
                func.lower(func.coalesce(BotUser.username, "")).like(pattern, escape="\\"),
                cast(BotUser.telegram_id, String).like(pattern, escape="\\"),
            )
        )
    if before:
        query = query.where(Order.id < int(before))
    found = (await session.execute(query.order_by(Order.id.desc()).limit(limit + 1))).all()
    has_more = len(found) > limit
    found = found[:limit]

    user_ids = [int(user.id) for _order, user, _service in found]
    stats = await _purchase_stats(session, user_ids, shop_id=shop_id)
    opted_out = await _opted_out_ids(session, user_ids, shop_id=shop_id)
    rows: list[TrialUserRow] = []
    for order, user, service in found:
        count, first_at, spent = stats.get(int(user.id), (0, None, 0))
        state, state_label = trial_state(service, now=now)
        rows.append(
            TrialUserRow(
                user_id=int(user.id),
                name=user.full_name or user.username or str(user.telegram_id),
                telegram_id=int(user.telegram_id),
                trial_order_id=int(order.id),
                trial_at=order.created_at,
                trial_state=state,
                trial_state_label=state_label,
                trial_ends_at=_as_utc(service.quota_expire_at) if service is not None else None,
                is_buyer=count > 0,
                purchase_count=count,
                first_purchase_at=first_at,
                total_spent=spent,
                is_blocked=bool(user.is_blocked),
                is_opted_out=int(user.id) in opted_out,
                can_manage=_can_manage(staff, user),
            )
        )
    return TrialUsersPage(rows=rows, next_before=rows[-1].trial_order_id if has_more and rows else None)
