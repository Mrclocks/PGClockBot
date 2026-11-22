"""Automatic discount rules: the best matching rule of the order's shop lowers a new order.

``apply_automatic_discounts`` runs where a customer order is created (plan, custom plan,
renewal, add-on), before the random discount. It picks the rule that gives the largest
reduction, claims one use of it and records the reduction in the order discount ledger, all
in the caller's transaction, so a rolled-back order leaves no trace.

Once-per-customer limits are held by a ledger ``claim_key`` under a partial unique index: the
first-purchase discount is ``first`` (shared by every first-purchase rule of the shop) and a
rule's per-customer uses are slots ``r<id>:<n>``. A claim whose order was later cancelled
(void, failed delivery, cleanup without release) is released before a new claim is tried.

A code typed later replaces a rule only when it gives more (``replace_rule_with_code``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscountRule, Order, OrderDiscount, OrderStatus, Plan, UserService
from app.services.cashback import order_event
from app.services.order_discounts import SOURCE_RULE, order_shop_key, record_order_discount
from app.services.random_discounts import apply_random_discount

AUDIENCE_ALL = "all"
AUDIENCE_FIRST = "first"
AUDIENCE_REPEAT = "repeat"
AUDIENCE_RETURNING = "returning"
AUDIENCE_EARLY_RENEWAL = "early_renewal"
AUDIENCES = (AUDIENCE_ALL, AUDIENCE_FIRST, AUDIENCE_REPEAT, AUDIENCE_RETURNING, AUDIENCE_EARLY_RENEWAL)
EVENT_NEW = "new"
EVENT_RENEW = "renew"
EVENT_ADDON = "addon"
EVENTS = (EVENT_NEW, EVENT_RENEW, EVENT_ADDON)
BPS = 10_000
# An automatic rule never takes more than this share of the price, so it never makes an order free.
MAX_REDUCTION_BPS = 9_000
RULE_LIMIT = 50
FIRST_PURCHASE_CLAIM = "first"
CODE_NOT_BETTER = "تخفیف خودکار این سفارش از این کد بیشتر است"


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """The customer's delivered purchases in the order's shop, before this order."""

    delivered_count: int
    last_delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class RuleCheckout:
    """What a rule looks at; ``amount`` is the price before any automatic reduction."""

    amount: int
    event: str
    plan_id: int | None
    history: CustomerHistory
    service_expires_at: datetime | None
    at: datetime


def _csv(value: str | None) -> set[str]:
    return {part.strip() for part in (value or "").split(",") if part.strip()}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_returning(rule: DiscountRule, checkout: RuleCheckout) -> bool:
    last = _aware(checkout.history.last_delivered_at)
    return last is not None and checkout.at - last >= timedelta(days=int(rule.audience_days or 0))


def _is_early_renewal(rule: DiscountRule, checkout: RuleCheckout) -> bool:
    expires = _aware(checkout.service_expires_at)
    if checkout.event != EVENT_RENEW or expires is None or expires <= checkout.at:
        return False
    return expires - checkout.at <= timedelta(days=int(rule.audience_days or 0))


_AUDIENCE_CHECKS: dict[str, Callable[[DiscountRule, RuleCheckout], bool]] = {
    AUDIENCE_ALL: lambda rule, checkout: True,
    AUDIENCE_FIRST: lambda rule, checkout: checkout.history.delivered_count == 0,
    AUDIENCE_REPEAT: lambda rule, checkout: checkout.history.delivered_count >= max(1, int(rule.min_purchases or 0)),
    AUDIENCE_RETURNING: _is_returning,
    AUDIENCE_EARLY_RENEWAL: _is_early_renewal,
}


def rule_applies(rule: DiscountRule, checkout: RuleCheckout) -> bool:
    ends = _aware(rule.ends_at)
    plans = _csv(rule.plan_ids)
    audience = _AUDIENCE_CHECKS.get(str(rule.audience))
    return bool(
        rule.enabled
        and audience is not None
        and (ends is None or checkout.at < ends)
        and checkout.event in _csv(rule.events)
        and (not plans or str(checkout.plan_id) in plans)
        and checkout.amount >= int(rule.min_order_amount or 0)
        and (rule.max_uses is None or int(rule.used_count or 0) < int(rule.max_uses))
        and audience(rule, checkout)
    )


def rule_discount(rule: DiscountRule, checkout: RuleCheckout) -> int:
    """The reduction one rule gives this checkout (0 when it does not apply)."""
    if checkout.amount <= 0 or not rule_applies(rule, checkout):
        return 0
    if rule.mode == "fixed":
        amount = max(0, int(rule.fixed_amount or 0))
    else:
        amount = checkout.amount * max(0, min(int(rule.percent_bps or 0), BPS)) // BPS
    if rule.max_per_order is not None:
        amount = min(amount, max(0, int(rule.max_per_order)))
    return min(amount, checkout.amount * MAX_REDUCTION_BPS // BPS)


def ranked_rules(rules: Iterable[DiscountRule], checkout: RuleCheckout) -> list[tuple[DiscountRule, int]]:
    """Matching rules, best first: larger reduction, then higher priority, then older rule."""
    paying = [(rule, rule_discount(rule, checkout)) for rule in rules]
    return sorted(
        ((rule, amount) for rule, amount in paying if amount > 0),
        key=lambda item: (-item[1], -int(item[0].priority or 0), int(item[0].id)),
    )


def _shop_rules(shop_id: int | None):
    return DiscountRule.shop_id.is_(None) if shop_id is None else DiscountRule.shop_id == int(shop_id)


def _shop_orders(shop_id: int | None):
    return Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)


async def active_rules(session: AsyncSession, shop_id: int | None) -> list[DiscountRule]:
    return list((await session.scalars(
        select(DiscountRule).where(_shop_rules(shop_id), DiscountRule.enabled.is_(True))
        .order_by(DiscountRule.priority.desc(), DiscountRule.id).limit(RULE_LIMIT)
    )).all())


async def customer_history(session: AsyncSession, order: Order) -> CustomerHistory:
    count, last = (await session.execute(
        select(func.count(), func.max(func.coalesce(Order.delivered_at, Order.created_at))).where(
            Order.user_id == int(order.user_id), _shop_orders(order.reseller_id),
            Order.status == OrderStatus.DELIVERED.value, Order.id != int(order.id),
        )
    )).one()
    return CustomerHistory(delivered_count=int(count or 0), last_delivered_at=last)


async def _service_expiry(session: AsyncSession, order: Order) -> datetime | None:
    if order.service_id is None:
        return None
    return await session.scalar(select(UserService.quota_expire_at).where(UserService.id == int(order.service_id)))


async def _is_exempt(session: AsyncSession, order: Order) -> bool:
    """Orders rules leave alone: free, already discounted, bulk, or a trial."""
    if int(order.amount or 0) <= 0 or order.discount_code or order.random_discount_id is not None:
        return True
    if int(order.quantity or 1) > 1:
        return True
    if order.plan_id is None:
        return False
    return bool(await session.scalar(select(Plan.is_trial).where(Plan.id == int(order.plan_id))))


async def _heal_dead_claims(session: AsyncSession, order: Order) -> None:
    """Release claims of this customer whose orders were cancelled without giving them back."""
    dead = select(Order.id).where(Order.user_id == int(order.user_id), Order.status == OrderStatus.CANCELLED.value)
    released = await session.execute(
        update(OrderDiscount)
        .where(
            OrderDiscount.shop_key == order_shop_key(order), OrderDiscount.user_id == int(order.user_id),
            OrderDiscount.claim_key.is_not(None), OrderDiscount.released_at.is_(None),
            OrderDiscount.order_id.in_(dead),
        )
        .values(released_at=datetime.now(timezone.utc))
        .returning(OrderDiscount.source, OrderDiscount.source_ref)
        .execution_options(synchronize_session=False)
    )
    for source, ref in released.all():
        if source == SOURCE_RULE and ref and ref.isdigit():
            await _give_back_rule_use(session, int(ref))


async def _held_claims(session: AsyncSession, order: Order) -> set[str]:
    return set((await session.scalars(
        select(OrderDiscount.claim_key).where(
            OrderDiscount.shop_key == order_shop_key(order), OrderDiscount.user_id == int(order.user_id),
            OrderDiscount.claim_key.is_not(None), OrderDiscount.released_at.is_(None),
        ).limit(RULE_LIMIT * 10)
    )).all())


def claim_for(rule: DiscountRule, held: set[str]) -> tuple[bool, str | None]:
    """(may use, claim key): the key the rule must hold (None for no per-customer limit)."""
    if rule.audience == AUDIENCE_FIRST:
        return FIRST_PURCHASE_CLAIM not in held, FIRST_PURCHASE_CLAIM
    if rule.customer_max_count is None:
        return True, None
    for slot in range(1, int(rule.customer_max_count) + 1):
        key = f"r{int(rule.id)}:{slot}"
        if key not in held:
            return True, key
    return False, None


async def _claim_rule_use(session: AsyncSession, rule: DiscountRule) -> bool:
    claimed = await session.execute(
        update(DiscountRule)
        .where(
            DiscountRule.id == int(rule.id), DiscountRule.enabled.is_(True),
            or_(DiscountRule.max_uses.is_(None), DiscountRule.used_count < DiscountRule.max_uses),
        )
        .values(used_count=DiscountRule.used_count + 1)
        .execution_options(synchronize_session=False)
    )
    return claimed.rowcount == 1


async def _give_back_rule_use(session: AsyncSession, rule_id: int) -> None:
    await session.execute(
        update(DiscountRule)
        .where(DiscountRule.id == int(rule_id), DiscountRule.used_count > 0)
        .values(used_count=DiscountRule.used_count - 1)
        .execution_options(synchronize_session=False)
    )


async def _take_rule(session: AsyncSession, order: Order, rule: DiscountRule, amount: int, claim: str | None) -> bool:
    """Claim one use of ``rule`` and its ledger line in a savepoint; False leaves nothing behind."""
    async with session.begin_nested() as savepoint:
        if not await _claim_rule_use(session, rule):
            await savepoint.rollback()
            return False
        if not await record_order_discount(
            session, order, source=SOURCE_RULE, amount=amount, source_ref=str(rule.id), claim_key=claim,
        ):
            await savepoint.rollback()
            return False
    return True


async def _rule_checkout(session: AsyncSession, order: Order) -> RuleCheckout:
    return RuleCheckout(
        amount=int(order.amount), event=order_event(order), plan_id=order.plan_id,
        history=await customer_history(session, order), service_expires_at=await _service_expiry(session, order),
        at=datetime.now(timezone.utc),
    )


async def apply_discount_rules(session: AsyncSession, order: Order) -> DiscountRule | None:
    """Lower a new order by the best rule of its shop it may still use; nothing is committed."""
    if order.id is None:
        await session.flush()
    if await _is_exempt(session, order):
        return None
    rules = await active_rules(session, order.reseller_id)
    if not rules:
        return None
    ranked = ranked_rules(rules, await _rule_checkout(session, order))
    if not ranked:
        return None
    await _heal_dead_claims(session, order)
    held = await _held_claims(session, order)
    for rule, amount in ranked:
        allowed, claim = claim_for(rule, held)
        if not allowed or not await _take_rule(session, order, rule, amount, claim):
            continue
        order.amount = int(order.amount) - amount
        order.discount_amount = int(order.discount_amount or 0) + amount
        await session.flush()
        return rule
    return None


async def apply_automatic_discounts(session: AsyncSession, order: Order, *, random: bool = True) -> None:
    """Every reduction a new order gets without a code: the best rule, then the random draw."""
    await apply_discount_rules(session, order)
    if random:
        await apply_random_discount(session, order)


async def live_rule_line(session: AsyncSession, order_id: int) -> OrderDiscount | None:
    return await session.scalar(select(OrderDiscount).where(
        OrderDiscount.order_id == int(order_id), OrderDiscount.source == SOURCE_RULE,
        OrderDiscount.released_at.is_(None),
    ))


async def release_rule_discount(session: AsyncSession, order_id: int) -> int:
    """Release the order's rule line and give its use back; returns the released amount (0 if none)."""
    line = await live_rule_line(session, order_id)
    if line is None:
        return 0
    released = await session.execute(
        update(OrderDiscount)
        .where(OrderDiscount.id == int(line.id), OrderDiscount.released_at.is_(None))
        .values(released_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if released.rowcount != 1:
        return 0
    if line.source_ref and line.source_ref.isdigit():
        await _give_back_rule_use(session, int(line.source_ref))
    return int(line.amount)


async def replace_rule_with_code(session: AsyncSession, order_id: int, code_discount: int) -> None:
    """Let a typed code replace the order's rule only when it gives more; raises ``ValueError``."""
    line = await live_rule_line(session, order_id)
    if line is None:
        return
    if code_discount <= int(line.amount):
        raise ValueError(CODE_NOT_BETTER)
    await release_rule_discount(session, order_id)


async def rule_name_for_order(session: AsyncSession, order: Order) -> str | None:
    """The name of the rule that lowered ``order`` (for the checkout summary), if any."""
    line = await live_rule_line(session, int(order.id))
    if line is None or not (line.source_ref or "").isdigit():
        return None
    return await session.scalar(select(DiscountRule.name).where(
        DiscountRule.id == int(line.source_ref), _shop_rules(order.reseller_id),
    ))
