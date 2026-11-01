"""Delete old orders and payments from the database by status, by hand or on a schedule.

Each shop (``None`` = the platform) picks the status tags to delete; the same tags apply to
the manual run and, when switched on, to the hourly run. A deleted order takes its payments
and every row that depends on them; references from history and configuration (code usage, trial claims, automations,
funnel events) are cleared instead, so a used code or a taken trial stays used. Customers'
services are never touched.

Never deleted, whatever the switches say:
* orders being paid or delivered (``paid``, ``delivering``): money is in flight;
* anything with an open gateway checkout or a gateway payment waiting for staff review,
  or a void still being processed: money may still arrive or be owed;
* payments of an order that is still open.

One order (with its payments) or one payment is one unit of work, committed alone and
re-checked at the point of deletion, so a second run at the same moment deletes nothing twice.
"""

from __future__ import annotations

import logging
from urllib.parse import quote
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentSettlement,
    PaymentStatus,
    PaymentVoid,
    ResellerSetting,
    SettlementStatus,
)
from app.services.authz import authz_from_staff, can_shop
from app.services.db_safe import rollback_quiet
from app.services.orders import release_unpaid_order_holds
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.user_purge import purge_row_references
from app.services.users import get_all_settings, set_settings_bulk
from app.services.ux20 import payment_shop_clause

logger = logging.getLogger(__name__)

PURGE_PERMISSION = "orders_purge"
KEY_TAGS = "cleanup_purge_tags"
KEY_AUTO = "cleanup_purge_auto"
KEY_DAYS = "cleanup_purge_after_days"
KEY_LAST_RUN = "cleanup_purge_last_run"
KEY_LAST_COUNT = "cleanup_purge_last_count"
KEY_LAST_KIND = "cleanup_purge_last_kind"
DEFAULT_DAYS = 30
MIN_DAYS = 1
MAX_DAYS = 3650
RULES_PATH = "/finance/cleanup/purge-rules"
RUN_PATH = "/finance/cleanup/purge-run"
# Rows deleted per run and shop; a larger backlog is finished by the next run.
BATCH = 200

ORDER = "order"
PAYMENT = "payment"
UNPAID_ORDER_STATUSES = frozenset(
    {OrderStatus.PENDING.value, OrderStatus.AWAITING_RECEIPT.value, OrderStatus.AWAITING_APPROVAL.value}
)
CLOSED_ORDER_STATUSES = frozenset(
    {OrderStatus.DELIVERED.value, OrderStatus.REJECTED.value, OrderStatus.CANCELLED.value}
)
REVIEW_PREFIX = "review:"


@dataclass(frozen=True, slots=True)
class PurgeTag:
    table: str
    status: str
    label: str
    locked: bool = False

    @property
    def token(self) -> str:
        return f"{self.table}:{self.status}"


ORDER_TAGS: tuple[PurgeTag, ...] = (
    PurgeTag(ORDER, OrderStatus.PENDING.value, "در انتظار"),
    PurgeTag(ORDER, OrderStatus.AWAITING_RECEIPT.value, "منتظر رسید"),
    PurgeTag(ORDER, OrderStatus.AWAITING_APPROVAL.value, "منتظر تأیید"),
    PurgeTag(ORDER, OrderStatus.PAID.value, "پرداخت‌شده", locked=True),
    PurgeTag(ORDER, OrderStatus.DELIVERING.value, "در حال تحویل", locked=True),
    PurgeTag(ORDER, OrderStatus.DELIVERED.value, "تحویل‌شده"),
    PurgeTag(ORDER, OrderStatus.REJECTED.value, "ردشده"),
    PurgeTag(ORDER, OrderStatus.CANCELLED.value, "لغوشده"),
)
PAYMENT_TAGS: tuple[PurgeTag, ...] = (
    PurgeTag(PAYMENT, PaymentStatus.PENDING.value, "در انتظار"),
    PurgeTag(PAYMENT, PaymentStatus.APPROVED.value, "تأییدشده"),
    PurgeTag(PAYMENT, PaymentStatus.REJECTED.value, "ردشده"),
    PurgeTag(PAYMENT, PaymentStatus.VOIDED.value, "باطل‌شده"),
)
TAGS: dict[str, PurgeTag] = {tag.token: tag for tag in (*ORDER_TAGS, *PAYMENT_TAGS)}


@dataclass(frozen=True, slots=True)
class PurgeRules:
    tags: frozenset[str]
    auto: bool
    days: int


@dataclass(frozen=True, slots=True)
class PurgeCounts:
    orders: int
    payments: int

    @property
    def total(self) -> int:
        return self.orders + self.payments


def parse_tags(raw: object) -> frozenset[str]:
    """Known, unlocked tag tokens from a saved CSV value; anything else is dropped."""
    tokens = {part.strip() for part in str(raw or "").split(",")}
    return frozenset(token for token in tokens if token in TAGS and not TAGS[token].locked)


def join_tags(tokens: Iterable[str]) -> str:
    return ",".join(sorted(parse_tags(",".join(tokens))))


def parse_days(raw: object) -> int:
    """Whole days between the limits; raises ``ValueError`` for anything else."""
    text = str(raw or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 5:
        raise ValueError(f"تعداد روز باید عدد صحیح بین {MIN_DAYS} و {MAX_DAYS} باشد")
    days = int(text)
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ValueError(f"تعداد روز باید عدد صحیح بین {MIN_DAYS} و {MAX_DAYS} باشد")
    return days


def rules_from_settings(settings: Mapping[str, str]) -> PurgeRules:
    try:
        days = parse_days(settings.get(KEY_DAYS, DEFAULT_DAYS))
    except ValueError:
        days = DEFAULT_DAYS
    auto = str(settings.get(KEY_AUTO) or "").strip().lower() in {"1", "true", "yes", "on"}
    return PurgeRules(tags=parse_tags(settings.get(KEY_TAGS)), auto=auto, days=days)


def purge_scope(staff: dict) -> int | None:
    """Shop whose rows ``staff`` may delete (``None`` = platform); raises ``PermissionError``."""
    if is_platform_admin(staff):
        return None
    if not can_shop(authz_from_staff(staff), PURGE_PERMISSION):
        raise PermissionError("دسترسی حذف کامل سفارش‌ها و پرداخت‌ها ندارید")
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


async def save_rules(session: AsyncSession, *, shop_id: int | None, rules: PurgeRules) -> None:
    await set_settings_bulk(
        session,
        {KEY_TAGS: join_tags(rules.tags), KEY_AUTO: "1" if rules.auto else "0", KEY_DAYS: str(rules.days)},
        reseller_id=shop_id,
    )


def _statuses(tags: frozenset[str], table: str) -> list[str]:
    """Statuses to delete in one table; locked tags are refused here too, whatever the caller passed."""
    return sorted(TAGS[t].status for t in parse_tags(",".join(tags)) if TAGS[t].table == table)


def _money_may_still_move(payment_id: ColumnElement[int]) -> ColumnElement[bool]:
    """An open gateway checkout, a gateway payment waiting for staff, or a void in progress."""
    open_checkout = exists(
        select(PaymentSettlement.id).where(
            PaymentSettlement.payment_id == payment_id,
            or_(
                PaymentSettlement.status.in_(
                    (SettlementStatus.CREATED.value, SettlementStatus.AWAITING.value, SettlementStatus.SETTLING.value)
                ),
                and_(
                    PaymentSettlement.status == SettlementStatus.FAILED.value,
                    PaymentSettlement.error_message.like(f"{REVIEW_PREFIX}%"),
                ),
            ),
        )
    )
    void_open = exists(select(PaymentVoid.id).where(PaymentVoid.payment_id == payment_id, PaymentVoid.status == "processing"))
    return or_(open_checkout, void_open)


def _order_shop(shop_id: int | None) -> ColumnElement[bool]:
    return Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)


def _order_query(shop_id: int | None, statuses: list[str], cutoff: datetime):
    busy = exists(select(Payment.id).where(Payment.order_id == Order.id, _money_may_still_move(Payment.id)))
    return select(Order.id).where(
        _order_shop(shop_id), Order.status.in_(statuses), Order.created_at <= cutoff, ~busy
    )


def _payment_query(shop_id: int | None, statuses: list[str], cutoff: datetime):
    order_closed = or_(Payment.order_id.is_(None), Order.status.in_(tuple(CLOSED_ORDER_STATUSES)))
    return (
        select(Payment.id)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            payment_shop_clause(shop_id), Payment.status.in_(statuses), Payment.created_at <= cutoff,
            order_closed, ~_money_may_still_move(Payment.id),
        )
    )


def _cutoff(days: int, now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


async def count_purgeable(
    session: AsyncSession, *, shop_id: int | None, tags: frozenset[str], days: int, now: datetime | None = None
) -> PurgeCounts:
    """How many rows a run with these tags would delete right now."""
    cutoff = _cutoff(days, now)
    counts = [0, 0]
    for index, (table, build) in enumerate(((ORDER, _order_query), (PAYMENT, _payment_query))):
        statuses = _statuses(tags, table)
        if statuses:
            query = build(shop_id, statuses, cutoff).subquery()
            counts[index] = int(await session.scalar(select(func.count()).select_from(query)) or 0)
    return PurgeCounts(orders=counts[0], payments=counts[1])


async def _delete_payments(session: AsyncSession, where: ColumnElement[bool]) -> int:
    await purge_row_references(session, Payment.__table__, where)
    return int((await session.execute(delete(Payment).where(where))).rowcount or 0)


async def _purge_one_order(session: AsyncSession, order_id: int, *, shop_id: int | None, statuses: list[str]) -> bool:
    """Delete one order with its payments; False when it changed meanwhile and was kept."""
    still = and_(Order.id == order_id, _order_shop(shop_id), Order.status.in_(statuses))
    order = (await session.execute(select(Order).where(still))).scalar_one_or_none()
    if order is None:
        return False
    if order.status in UNPAID_ORDER_STATUSES:
        # Same claim as a cancel: only the run that moves it to cancelled releases its holds.
        claimed = await session.execute(
            update(Order).where(still).values(status=OrderStatus.CANCELLED.value).execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await rollback_quiet(session)
            return False
        await release_unpaid_order_holds(session, order, reason="deleted by cleanup")
        statuses = [*statuses, OrderStatus.CANCELLED.value]
    busy = await session.scalar(
        select(Payment.id).where(Payment.order_id == order_id, _money_may_still_move(Payment.id)).limit(1)
    )
    if busy is not None:
        await rollback_quiet(session)
        return False
    await _delete_payments(session, Payment.order_id == order_id)
    await purge_row_references(session, Order.__table__, Order.id == order_id)
    deleted = await session.execute(
        delete(Order).where(Order.id == order_id, _order_shop(shop_id), Order.status.in_(statuses))
    )
    if deleted.rowcount != 1:
        await rollback_quiet(session)
        return False
    await session.commit()
    return True


async def _purge_one_payment(session: AsyncSession, payment_id: int, *, shop_id: int | None, statuses: list[str], cutoff: datetime) -> bool:
    """Delete one payment when it still matches the rules at this moment."""
    still = Payment.id.in_(_payment_query(shop_id, statuses, cutoff).where(Payment.id == payment_id))
    if await _delete_payments(session, still) != 1:
        await rollback_quiet(session)
        return False
    await session.commit()
    return True


async def run_purge(
    session: AsyncSession, *, shop_id: int | None, tags: frozenset[str], days: int, now: datetime | None = None
) -> PurgeCounts:
    """Delete up to ``BATCH`` orders and ``BATCH`` payments of one shop; each row is its own unit."""
    cutoff = _cutoff(days, now)
    order_statuses = _statuses(tags, ORDER)
    payment_statuses = _statuses(tags, PAYMENT)
    orders = payments = 0
    if order_statuses:
        ids = list((await session.scalars(_order_query(shop_id, order_statuses, cutoff).order_by(Order.id).limit(BATCH))).all())
        for order_id in ids:
            try:
                orders += int(await _purge_one_order(session, int(order_id), shop_id=shop_id, statuses=order_statuses))
            except Exception:
                logger.exception("order purge failed order=%s shop=%s", order_id, shop_id)
                await rollback_quiet(session)
    if payment_statuses:
        ids = list((await session.scalars(_payment_query(shop_id, payment_statuses, cutoff).order_by(Payment.id).limit(BATCH))).all())
        for payment_id in ids:
            try:
                payments += int(
                    await _purge_one_payment(session, int(payment_id), shop_id=shop_id, statuses=payment_statuses, cutoff=cutoff)
                )
            except Exception:
                logger.exception("payment purge failed payment=%s shop=%s", payment_id, shop_id)
                await rollback_quiet(session)
    return PurgeCounts(orders=orders, payments=payments)


async def record_purge_run(session: AsyncSession, *, shop_id: int | None, kind: str, counts: PurgeCounts) -> None:
    await set_settings_bulk(
        session,
        {
            KEY_LAST_RUN: datetime.now(timezone.utc).isoformat(timespec="minutes"),
            KEY_LAST_COUNT: f"{counts.orders},{counts.payments}",
            KEY_LAST_KIND: kind,
        },
        reseller_id=shop_id,
    )


@dataclass(frozen=True, slots=True)
class PurgeRow:
    """One status tag and whether it is switched on for deletion."""

    tag: PurgeTag
    on: bool


@dataclass(frozen=True, slots=True)
class PurgeSection:
    title: str
    rows: tuple[PurgeRow, ...]


def purge_sections(rules: PurgeRules) -> tuple[PurgeSection, ...]:
    def rows(tags: tuple[PurgeTag, ...]) -> tuple[PurgeRow, ...]:
        return tuple(PurgeRow(tag=t, on=t.token in rules.tags) for t in tags)

    return (PurgeSection("سفارش‌ها", rows(ORDER_TAGS)), PurgeSection("پرداخت‌ها", rows(PAYMENT_TAGS)))


def _last_run_view(settings: Mapping[str, str]) -> dict[str, object] | None:
    raw = str(settings.get(KEY_LAST_RUN) or "").strip()
    if not raw:
        return None
    orders, _, payments = str(settings.get(KEY_LAST_COUNT) or "0,0").partition(",")
    kind = "دستی" if settings.get(KEY_LAST_KIND) == "manual" else "خودکار"
    return {
        "at": raw.replace("T", " ")[:16],
        "kind": kind,
        "orders": int(orders) if orders.isdigit() else 0,
        "payments": int(payments) if payments.isdigit() else 0,
    }


async def purge_panel_context(
    session: AsyncSession, staff: dict, settings: Mapping[str, str], *, return_to: str
) -> dict[str, object] | None:
    """Template values for the cleanup tab, or None when ``staff`` may not delete rows."""
    try:
        shop_id = purge_scope(staff)
    except PermissionError:
        return None
    rules = rules_from_settings(settings)
    return {
        "sections": purge_sections(rules),
        "days": rules.days,
        "min_days": MIN_DAYS,
        "max_days": MAX_DAYS,
        "ready": await count_purgeable(session, shop_id=shop_id, tags=rules.tags, days=rules.days),
        "auto_on": rules.auto,
        "last_run": _last_run_view(settings),
        "rules_action": f"{RULES_PATH}?next={quote(return_to)}",
        "run_action": f"{RUN_PATH}?next={quote(return_to)}",
    }


SHOP_SCAN_LIMIT = 2000


async def _shops_with_auto_rules(session: AsyncSession) -> list[int]:
    rows = await session.scalars(
        select(ResellerSetting.reseller_user_id)
        .where(ResellerSetting.key == KEY_AUTO, ResellerSetting.value == "1")
        .order_by(ResellerSetting.reseller_user_id)
        .limit(SHOP_SCAN_LIMIT)
    )
    return [int(shop_id) for shop_id in rows.all()]


async def auto_purge_all_shops(session: AsyncSession) -> int:
    """One automatic pass over the platform and every shop that switched it on; one failure does not stop the rest."""
    total = 0
    for shop_id in [None, *await _shops_with_auto_rules(session)]:
        try:
            rules = rules_from_settings(await get_all_settings(session, reseller_id=shop_id))
            if not rules.auto or not rules.tags:
                continue
            done = await run_purge(session, shop_id=shop_id, tags=rules.tags, days=rules.days)
            if done.total:
                await record_purge_run(session, shop_id=shop_id, kind="auto", counts=done)
            total += done.total
        except Exception:
            logger.exception("automatic purge failed shop=%s", shop_id)
            await rollback_quiet(session)
    return total
