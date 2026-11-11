"""Shop cashback: part of a delivered purchase returns to the buyer's wallet in that shop.

Each shop (``shop_id`` NULL for the platform shop) runs its own rules and pays its own
cashback; the money only ever moves inside that shop's wallet purse.

1. ``grant_for_order`` runs after a delivery (next to loyalty points). It picks the best
   rule (or the sum, per shop setting), applies the per-customer monthly caps, claims the
   shop's monthly budget and stores one ``pending`` grant per order, all in one commit.
2. ``mature_grants`` (a job) credits grants whose hold has passed, once each.
3. ``reverse_order_cashback`` runs when a purchase is voided or cancelled with a refund:
   a pending grant is cancelled; a credited one is taken back as far as the wallet allows
   and the rest is recorded as unrecoverable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CashbackBudget, CashbackGrant, CashbackRule, Order, OrderStatus
from app.py_compat import StrEnum
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman
from app.services.platform_identity import deliverable_telegram_id
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user
from app.services.users import get_setting
from app.services.wallet import credit_wallet, debit_wallet, get_wallet_balance

logger = logging.getLogger(__name__)

RULE_LIMIT = 50
MATURE_BATCH = 500
BPS = 10_000
DEFAULT_HOLD_HOURS = 24
MAX_HOLD_HOURS = 24 * 90
# Below Telegram's per-bot limit (about 30 messages a second).
SEND_INTERVAL_SECONDS = 0.05


class GrantStatus(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    CANCELLED = "cancelled"
    REVERSED = "reversed"


EVENT_NEW = "new"
EVENT_RENEW = "renew"
EVENT_ADDON = "addon"


@dataclass(frozen=True, slots=True)
class Purchase:
    """What a rule looks at; ``amount`` is the final price paid, after discounts."""

    amount: int
    event: str
    plan_id: int | None
    payment_method: str
    has_discount: bool
    first_purchase: bool
    at: datetime


def _csv(value: str | None) -> set[str]:
    return {part.strip() for part in (value or "").split(",") if part.strip()}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rule_applies(rule: CashbackRule, purchase: Purchase) -> bool:
    starts, ends = _aware(rule.starts_at), _aware(rule.ends_at)
    plans, methods = _csv(rule.plan_ids), _csv(rule.payment_methods)
    return bool(
        rule.enabled
        and (starts is None or purchase.at >= starts)
        and (ends is None or purchase.at < ends)
        and purchase.event in _csv(rule.events)
        and (not plans or str(purchase.plan_id) in plans)
        and (not methods or purchase.payment_method in methods)
        and purchase.amount >= int(rule.min_order_amount or 0)
        and (rule.allow_with_discount or not purchase.has_discount)
        and (not rule.first_purchase_only or purchase.first_purchase)
    )


def rule_amount(rule: CashbackRule, purchase: Purchase) -> int:
    """Cashback one rule gives this purchase (0 when it does not apply); never above the price."""
    if purchase.amount <= 0 or not _rule_applies(rule, purchase):
        return 0
    if rule.mode == "fixed":
        amount = max(0, int(rule.fixed_amount or 0))
    else:
        amount = purchase.amount * max(0, min(int(rule.percent_bps or 0), BPS)) // BPS
    if rule.max_per_order is not None:
        amount = min(amount, max(0, int(rule.max_per_order)))
    return min(amount, purchase.amount)


def combine(amounts: list[tuple[CashbackRule, int]], *, mode: str, price: int) -> tuple[int | None, int]:
    """(rule id, amount): the best rule, or the sum of all (capped at the price)."""
    paying = [(rule, amount) for rule, amount in amounts if amount > 0]
    if not paying:
        return None, 0
    if mode == "sum":
        return None, min(price, sum(amount for _, amount in paying))
    rule, amount = max(paying, key=lambda item: (item[1], -int(item[0].priority or 0)))
    return int(rule.id), amount


def order_event(order: Order) -> str:
    note = (order.note or "").strip()
    if note.startswith("renew:"):
        return EVENT_RENEW
    if note.startswith("svc_addon:"):
        return EVENT_ADDON
    return EVENT_NEW


async def _setting(session: AsyncSession, key: str, default: str, shop_id: int | None) -> str:
    return str(await get_setting(session, key, default, reseller_id=shop_id) or default)


async def cashback_enabled(session: AsyncSession, shop_id: int | None) -> bool:
    return (await _setting(session, "cashback_enabled", "0", shop_id)).strip().lower() in {"1", "true", "on", "yes"}


async def _int_setting(session: AsyncSession, key: str, default: int, shop_id: int | None, *, upper: int) -> int:
    try:
        value = int(await _setting(session, key, str(default), shop_id))
    except ValueError:
        return default
    return value if 0 <= value <= upper else default


async def _rules(session: AsyncSession, shop_id: int | None) -> list[CashbackRule]:
    scope = CashbackRule.shop_id.is_(None) if shop_id is None else CashbackRule.shop_id == int(shop_id)
    return list((await session.scalars(
        select(CashbackRule).where(scope, CashbackRule.enabled.is_(True))
        .order_by(CashbackRule.priority.desc(), CashbackRule.id).limit(RULE_LIMIT)
    )).all())


def _shop_scope(column, shop_id: int | None):
    return column.is_(None) if shop_id is None else column == int(shop_id)


async def _first_purchase(session: AsyncSession, order: Order) -> bool:
    earlier = await session.scalar(
        select(func.count()).select_from(Order).where(
            Order.user_id == int(order.user_id), _shop_scope(Order.reseller_id, order.reseller_id),
            Order.status == OrderStatus.DELIVERED.value, Order.id != int(order.id),
        )
    )
    return not earlier


def _month_start(at: datetime) -> datetime:
    return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _within_customer_caps(session: AsyncSession, rule: CashbackRule, order: Order, amount: int,
                                *, at: datetime) -> bool:
    if rule.customer_max_count is None and rule.customer_max_amount is None:
        return True
    count, total = (await session.execute(
        select(func.count(), func.coalesce(func.sum(CashbackGrant.amount), 0)).where(
            CashbackGrant.user_id == int(order.user_id), _shop_scope(CashbackGrant.shop_id, order.reseller_id),
            CashbackGrant.rule_id == int(rule.id), CashbackGrant.created_at >= _month_start(at),
            CashbackGrant.status.in_([str(GrantStatus.PENDING), str(GrantStatus.AVAILABLE)]),
        )
    )).one()
    if rule.customer_max_count is not None and int(count) >= int(rule.customer_max_count):
        return False
    return rule.customer_max_amount is None or int(total) + amount <= int(rule.customer_max_amount)


async def quote_cashback(session: AsyncSession, order: Order, *, at: datetime) -> tuple[int | None, int]:
    """The cashback this order earns now (rule id, amount); (None, 0) when none."""
    shop_id = order.reseller_id
    if int(order.amount or 0) <= 0 or not await cashback_enabled(session, shop_id):
        return None, 0
    purchase = Purchase(
        amount=int(order.amount), event=order_event(order), plan_id=order.plan_id,
        payment_method=str(order.payment_method or ""), has_discount=bool(order.discount_amount or order.discount_code),
        first_purchase=await _first_purchase(session, order), at=at,
    )
    amounts: list[tuple[CashbackRule, int]] = []
    for rule in await _rules(session, shop_id):
        amount = rule_amount(rule, purchase)
        if amount and await _within_customer_caps(session, rule, order, amount, at=at):
            amounts.append((rule, amount))
    mode = await _setting(session, "cashback_combine", "best", shop_id)
    return combine(amounts, mode=mode, price=purchase.amount)


async def _claim_budget(session: AsyncSession, shop_id: int | None, amount: int, *, at: datetime) -> bool:
    """Add ``amount`` to this month's use if the budget allows (0 = no budget); caller commits."""
    budget = await _int_setting(session, "cashback_monthly_budget", 0, shop_id, upper=2_000_000_000)
    if budget == 0:
        return True
    key, month = int(shop_id or 0), at.strftime("%Y-%m")
    try:
        async with session.begin_nested():
            session.add(CashbackBudget(shop_key=key, month=month, used=0))
            await session.flush()
    except IntegrityError:
        logger.debug("cashback budget row exists shop=%s month=%s", key, month)
    result = await session.execute(
        update(CashbackBudget)
        .where(CashbackBudget.shop_key == key, CashbackBudget.month == month,
               CashbackBudget.used + int(amount) <= budget)
        .values(used=CashbackBudget.used + int(amount))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def grant_for_order(session: AsyncSession, order: Order, *, now: datetime | None = None) -> CashbackGrant | None:
    """Store this delivered order's cashback once; ``None`` when it earns none."""
    now = now or datetime.now(timezone.utc)
    if order.status != OrderStatus.DELIVERED.value:
        return None
    # Captured first: a rollback below expires ``order``.
    order_id, shop_id, user_id, price = int(order.id), order.reseller_id, int(order.user_id), int(order.amount)
    existing = await session.scalar(select(CashbackGrant).where(CashbackGrant.order_id == order_id))
    if existing is not None:
        return existing
    rule_id, amount = await quote_cashback(session, order, at=now)
    if amount <= 0:
        return None
    if not await _claim_budget(session, shop_id, amount, at=now):
        await session.rollback()
        logger.info("cashback budget exhausted shop=%s order=%s", shop_id, order_id)
        return None
    hours = await _int_setting(session, "cashback_hold_hours", DEFAULT_HOLD_HOURS, shop_id, upper=MAX_HOLD_HOURS)
    grant = CashbackGrant(
        shop_id=shop_id, user_id=user_id, order_id=order_id, rule_id=rule_id,
        base_amount=price, amount=amount, status=str(GrantStatus.PENDING),
        available_at=now + timedelta(hours=hours),
    )
    try:
        async with session.begin_nested():
            session.add(grant)
            await session.flush()
    except IntegrityError:
        # A concurrent run granted this order; undo this run's budget claim with it.
        await session.rollback()
        return await session.scalar(select(CashbackGrant).where(CashbackGrant.order_id == order_id))
    await session.commit()
    return grant


async def _claim_status(session: AsyncSession, grant: CashbackGrant, *, frm: GrantStatus, to: GrantStatus,
                        at: datetime) -> bool:
    result = await session.execute(
        update(CashbackGrant)
        .where(CashbackGrant.id == int(grant.id), CashbackGrant.status == str(frm))
        .values(status=str(to), resolved_at=at if to != GrantStatus.AVAILABLE else None)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def mature_grants(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Credit every grant whose hold has passed, once each; one bad row never stops the batch."""
    now = now or datetime.now(timezone.utc)
    due = (await session.scalars(
        select(CashbackGrant)
        .where(CashbackGrant.status == str(GrantStatus.PENDING), CashbackGrant.available_at <= now)
        .order_by(CashbackGrant.id).limit(MATURE_BATCH)
    )).all()
    credited: list[CashbackGrant] = []
    for grant in due:
        try:
            if not await _claim_status(session, grant, frm=GrantStatus.PENDING, to=GrantStatus.AVAILABLE, at=now):
                continue
            user = await session.get(BotUser, int(grant.user_id))
            # One commit for the claim and the credit: a failure rolls both back.
            await credit_wallet(session, user, int(grant.amount), f"کش‌بک سفارش #{int(grant.order_id)}",
                                shop_id=grant.shop_id, commit=True, idempotency_key=f"cashback:{int(grant.id)}")
            credited.append(grant)
        except Exception:  # noqa: BLE001 - per-row isolation in a background job; the row stays pending
            logger.warning("cashback credit failed grant=%s", grant.id, exc_info=True)
            await rollback_quiet(session)
    await notify_credited(session, credited)
    return len(credited)


def credited_text(grant: CashbackGrant) -> str:
    return (
        f"🎁 <b>کش‌بک {format_toman(int(grant.amount))}</b> به کیف پول شما اضافه شد\n"
        f"برای سفارش #{int(grant.order_id)}"
    )


async def _send_shop_batch(session: AsyncSession, shop_id: int | None, grants: list[CashbackGrant]) -> None:
    users = {int(u.id): u for u in (await session.scalars(
        select(BotUser).where(BotUser.id.in_({int(g.user_id) for g in grants})))).all()}
    first = users.get(int(grants[0].user_id))
    if first is None:
        return
    try:
        bot, should_close = await open_notify_bot_for_user(session, first, shop_id=shop_id)
    except ShopBotUnavailable:
        logger.info("cashback notice skipped: shop bot unavailable shop=%s", shop_id)
        return
    try:
        for grant in grants:
            chat_id = deliverable_telegram_id(getattr(users.get(int(grant.user_id)), "telegram_id", None))
            if chat_id is None:
                continue
            try:
                await bot.send_message(chat_id, credited_text(grant), parse_mode="HTML")
            except Exception:  # noqa: BLE001 - one undeliverable chat never stops the others; logged
                logger.debug("cashback notice failed grant=%s", grant.id, exc_info=True)
            await asyncio.sleep(SEND_INTERVAL_SECONDS)
    finally:
        if should_close:
            await bot.session.close()


async def notify_credited(session: AsyncSession, grants: list[CashbackGrant]) -> None:
    """Tell each buyer once, after the credits committed: one bot per shop, paced under Telegram limits."""
    by_shop: dict[int | None, list[CashbackGrant]] = {}
    for grant in grants:
        by_shop.setdefault(grant.shop_id, []).append(grant)
    for shop_id, batch in by_shop.items():
        try:
            await _send_shop_batch(session, shop_id, batch)
        except Exception:  # noqa: BLE001 - notices are optional; the money is already credited
            logger.warning("cashback notices failed shop=%s", shop_id, exc_info=True)


async def reverse_order_cashback(session: AsyncSession, order_id: int, *, reason: str,
                                 now: datetime | None = None) -> CashbackGrant | None:
    """Undo an order's cashback after a void or a refunded cancellation; safe to call twice."""
    now = now or datetime.now(timezone.utc)
    grant = await session.scalar(select(CashbackGrant).where(CashbackGrant.order_id == int(order_id)))
    if grant is None:
        return None
    if await _claim_status(session, grant, frm=GrantStatus.PENDING, to=GrantStatus.CANCELLED, at=now):
        await session.commit()
        return grant
    if not await _claim_status(session, grant, frm=GrantStatus.AVAILABLE, to=GrantStatus.REVERSED, at=now):
        return grant
    user = await session.get(BotUser, int(grant.user_id))
    balance = await get_wallet_balance(session, user, shop_id=grant.shop_id)
    recoverable = max(0, min(int(grant.amount), int(balance)))
    await session.execute(
        update(CashbackGrant).where(CashbackGrant.id == int(grant.id))
        .values(reversed_amount=recoverable, unrecoverable_amount=int(grant.amount) - recoverable)
        .execution_options(synchronize_session=False)
    )
    if recoverable:
        # One commit for the claim, the amounts and the debit: a failure rolls all back.
        await debit_wallet(session, user, recoverable, f"برگشت کش‌بک سفارش #{int(order_id)}: {reason}"[:255],
                           shop_id=grant.shop_id, commit=True, idempotency_key=f"cashback-reverse:{int(grant.id)}")
    else:
        await session.commit()
    return grant


async def reverse_service_cashback(session: AsyncSession, service_id: int, *, reason: str) -> int:
    """Reverse the cashback of every order of a service (cancellation with refund)."""
    order_ids = (await session.scalars(
        select(Order.id).where(Order.service_id == int(service_id)).limit(RULE_LIMIT)
    )).all()
    reversed_count = 0
    for order_id in order_ids:
        reversed_count += int(await reverse_order_cashback(session, int(order_id), reason=reason) is not None)
    return reversed_count


async def grant_after_delivery(session: AsyncSession, order: Order) -> None:
    """Best effort after a committed delivery: a cashback failure never undoes the sale."""
    order_id = int(order.id)
    try:
        await grant_for_order(session, order)
    except Exception:  # noqa: BLE001 - optional reward after a committed delivery; logged
        logger.warning("cashback grant failed order=%s", order_id, exc_info=True)
        await rollback_quiet(session)


async def reverse_after_refund(session: AsyncSession, *, service_id: int | None = None,
                               order_id: int | None = None, reason: str) -> None:
    """Best effort after a committed void or refunded cancellation; logged, never raised."""
    try:
        if order_id is not None:
            await reverse_order_cashback(session, int(order_id), reason=reason)
        if service_id is not None:
            await reverse_service_cashback(session, int(service_id), reason=reason)
    except Exception:  # noqa: BLE001 - the refund already committed; a failed reversal is logged
        logger.warning("cashback reversal failed order=%s service=%s", order_id, service_id, exc_info=True)
        await rollback_quiet(session)
