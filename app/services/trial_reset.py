"""«ریست کاربران تست»: give one group of a shop's trial takers their free trial back in a single step.

A group is every trial taker of the shop, the ones who bought, or the ones who never bought. Resetting deletes
the shop's ``TrialClaim`` of each customer in the group who can take a trial again: not blocked, and with no
trial of this shop still running (otherwise they would hold two at once). Orders and services stay.

The reset is one ``DELETE`` bounded by the number the staff member confirmed, so a customer who became ready
after the page was drawn is never reset without being counted, and a repeated submit deletes nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from sqlalchemy import and_, delete, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import BotUser, Order, Plan, TrialClaim, UserService
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.trial_user_actions import trial_shop_key
from app.services.trial_users import ENDED_TRIAL_STATUSES, FILTER_LABELS, is_buyer_clause, trial_orders_subquery

RetrialGroup = Literal["all", "buyers", "non_buyers"]
RETRIAL_GROUPS: tuple[RetrialGroup, ...] = ("all", "buyers", "non_buyers")
# The confirmed count travels in a form field; anything outside the column range is refused.
MAX_CONFIRMED = 2**31 - 1


@dataclass(frozen=True, slots=True)
class RetrialGroupCount:
    """One group as the page shows it: how many took the trial, and how many a reset would reach now."""

    key: RetrialGroup
    label: str
    trials: int
    ready: int


def parse_group(raw: str) -> RetrialGroup:
    if raw in RETRIAL_GROUPS:
        return cast(RetrialGroup, raw)
    raise ValueError("گروه کاربران نامعتبر است")


def parse_confirmed(raw: object) -> int:
    """The count shown in the confirm dialog; a reset never reaches more customers than this."""
    text = str(raw or "").strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 10:
        raise ValueError("تعداد تأییدشده نامعتبر است؛ صفحه را تازه کنید")
    value = int(text)
    if not 0 < value <= MAX_CONFIRMED:
        raise ValueError("تعداد تأییدشده نامعتبر است؛ صفحه را تازه کنید")
    return value


def _trial_ended(service: type[UserService], now: datetime) -> ColumnElement[bool]:
    """SQL twin of ``trial_users.trial_state(...) == "ended"``, plus a missing or cancelled service."""
    status = func.lower(func.trim(func.coalesce(service.quota_status, "")))
    return or_(
        service.id.is_(None),
        service.is_cancelled.is_(True),
        and_(
            service.quota_synced_at.is_not(None),
            or_(
                status.in_(sorted(ENDED_TRIAL_STATUSES)),
                and_(service.quota_expire_at.is_not(None), service.quota_expire_at <= now),
            ),
        ),
    )


def _has_running_trial(shop_id: int | None, user_column, now: datetime) -> ColumnElement[bool]:
    """Any delivered trial of this shop whose service has not ended, not only the first one."""
    order = aliased(Order)
    service = aliased(UserService)
    shop = order.reseller_id.is_(None) if shop_id is None else order.reseller_id == int(shop_id)
    return exists(
        select(order.id)
        .join(Plan, Plan.id == order.plan_id)
        .outerjoin(service, service.id == order.service_id)
        .where(
            order.user_id == user_column,
            shop,
            order.status == "delivered",
            Plan.is_trial.is_(True),
            ~_trial_ended(service, now),
        )
    )


def _ready_clause(shop_id: int | None, user_column, now: datetime) -> ColumnElement[bool]:
    """The customer holds this shop's claim, is not blocked and has no trial of this shop running."""
    holds_claim = exists(
        select(TrialClaim.id).where(TrialClaim.user_id == user_column, TrialClaim.shop_key == trial_shop_key(shop_id))
    )
    return and_(holds_claim, BotUser.is_blocked.is_(False), ~_has_running_trial(shop_id, user_column, now))


def _group_clause(group: RetrialGroup, shop_id: int | None, user_column) -> ColumnElement[bool]:
    if group == "buyers":
        return is_buyer_clause(shop_id, user_column)
    if group == "non_buyers":
        return ~is_buyer_clause(shop_id, user_column)
    return literal(True)


async def retrial_group_counts(
    session: AsyncSession, *, shop_id: int | None, now: datetime | None = None
) -> tuple[RetrialGroupCount, ...]:
    """Trial takers and ready customers of every group, in one aggregate query."""
    now = now or datetime.now(timezone.utc)
    trials = trial_orders_subquery(shop_id)
    buyer = is_buyer_clause(shop_id, trials.c.user_id)
    ready = _ready_clause(shop_id, trials.c.user_id, now)
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(buyer),
                func.count().filter(ready),
                func.count().filter(and_(ready, buyer)),
            )
            .select_from(trials)
            .join(BotUser, BotUser.id == trials.c.user_id)
        )
    ).one()
    total, buyers, ready_total, ready_buyers = (int(value or 0) for value in row)
    counts = {
        "all": (total, ready_total),
        "buyers": (buyers, ready_buyers),
        "non_buyers": (total - buyers, ready_total - ready_buyers),
    }
    return tuple(
        RetrialGroupCount(key=key, label=FILTER_LABELS[key], trials=counts[key][0], ready=counts[key][1])
        for key in RETRIAL_GROUPS
    )


async def reset_trial_group(
    session: AsyncSession,
    *,
    staff: dict,
    shop_id: int | None,
    group: RetrialGroup,
    confirmed: int,
    now: datetime | None = None,
) -> int:
    """Delete the claims of at most ``confirmed`` ready customers of ``group``; returns how many were reset."""
    now = now or datetime.now(timezone.utc)
    trials = trial_orders_subquery(shop_id)
    targets = (
        select(TrialClaim.id)
        .join(trials, trials.c.user_id == TrialClaim.user_id)
        .join(BotUser, BotUser.id == TrialClaim.user_id)
        .where(
            TrialClaim.shop_key == trial_shop_key(shop_id),
            BotUser.is_blocked.is_(False),
            ~_has_running_trial(shop_id, TrialClaim.user_id, now),
            _group_clause(group, shop_id, TrialClaim.user_id),
        )
        .order_by(TrialClaim.id)
        .limit(confirmed)
    )
    result = await session.execute(
        delete(TrialClaim).where(TrialClaim.id.in_(targets.scalar_subquery())).execution_options(synchronize_session=False)
    )
    reset = int(result.rowcount or 0)
    if reset:
        add_staff_action(
            session, actor_from_staff(staff, shop_id=shop_id), action="trial_reset", target_type="customer",
            target_id=None, reason=f"{reset} کاربر · {FILTER_LABELS[group]}",
        )
    await session.commit()
    return reset
