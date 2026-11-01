"""Approve a card receipt that nobody reviewed for the shop's chosen number of minutes.

Each shop (``None`` = the platform) decides in its own settings; off by default. Only a new
service purchase paid by card qualifies: not a wallet top-up, renewal, add-on or agency fee. A
receipt that is a duplicate of another payment's, or whose fingerprint is unknown, stays for a
person. Approval itself goes through ``approve_payment``, whose atomic claim makes a second
attempt harmless.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentMethod, PaymentReceiptFingerprint, PaymentStatus
from app.services.db_safe import rollback_quiet
from app.services.receipts import auto_approve_receipt

logger = logging.getLogger(__name__)

MINUTES_KEY = "auto_approve_after_minutes"
MAX_AMOUNT_KEY = "auto_approve_after_max_amount"
RETURNING_ONLY_KEY = "auto_approve_after_returning_only"
MAX_MINUTES = 24 * 60
BATCH_LIMIT = 50
SHOP_SCAN_LIMIT = 500
MUTATION_NOTE_PREFIXES = ("renew:", "svc_addon:", "reseller_app:")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    minutes: int
    max_amount: int
    returning_only: bool

    @property
    def reason(self) -> str:
        return f"بدون بررسی ادمین تا {self.minutes} دقیقه"


def _whole_number(raw: object) -> int:
    text = str(raw or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 12 else 0


def rule_from_settings(settings: Mapping[str, str]) -> ApprovalRule | None:
    """The shop's rule, or None while it is off (0, empty or any malformed value)."""
    minutes = _whole_number(settings.get(MINUTES_KEY))
    if minutes < 1:
        return None
    return ApprovalRule(
        minutes=min(minutes, MAX_MINUTES),
        max_amount=_whole_number(settings.get(MAX_AMOUNT_KEY)),
        returning_only=str(settings.get(RETURNING_ONLY_KEY, "1")).strip().lower() in TRUE_VALUES,
    )


def _shop_clause(shop_id: int | None) -> ColumnElement[bool]:
    return Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)


def _waiting_clause() -> ColumnElement[bool]:
    """A card receipt of a service purchase, attached and still undecided."""
    return and_(
        Payment.status == PaymentStatus.PENDING.value,
        Payment.method == PaymentMethod.CARD.value,
        Payment.is_wallet_topup.is_(False),
        Payment.receipt_at.is_not(None),
        Payment.receipt_file_id.is_not(None),
        Order.status == OrderStatus.AWAITING_APPROVAL.value,
        or_(Order.note.is_(None), and_(*[~Order.note.like(f"{prefix}%") for prefix in MUTATION_NOTE_PREFIXES])),
    )


async def shops_with_waiting_receipts(session: AsyncSession) -> list[int | None]:
    """Shops that have an undecided card receipt right now (None = the platform), bounded."""
    rows = await session.execute(
        select(Order.reseller_id)
        .join(Payment, Payment.order_id == Order.id)
        .where(_waiting_clause())
        .distinct()
        .limit(SHOP_SCAN_LIMIT)
    )
    return [None if rid is None else int(rid) for (rid,) in rows.all()]


def _returning_customer(order: type[Order], shop_id: int | None) -> ColumnElement[bool]:
    earlier = aliased(Order)
    return exists(
        select(earlier.id).where(
            earlier.user_id == order.user_id,
            earlier.id != order.id,
            earlier.status == OrderStatus.DELIVERED.value,
            earlier.amount > 0,
            earlier.reseller_id.is_(None) if shop_id is None else earlier.reseller_id == int(shop_id),
        )
    )


def _is_unique_receipt(payment: type[Payment]) -> ColumnElement[bool]:
    """The payment has a fingerprint and no other payment shares its file or image."""
    mine = aliased(PaymentReceiptFingerprint)
    other = aliased(PaymentReceiptFingerprint)
    has_one = exists(
        select(mine.id).where(
            mine.payment_id == payment.id,
            or_(mine.file_unique_id.is_not(None), mine.sha256.is_not(None)),
        )
    )
    shares = exists(
        select(other.id)
        .join(mine, mine.payment_id == payment.id)
        .where(
            other.payment_id != payment.id,
            or_(
                and_(mine.file_unique_id.is_not(None), other.file_unique_id == mine.file_unique_id),
                and_(mine.sha256.is_not(None), other.sha256 == mine.sha256),
            ),
        )
    )
    return and_(has_one, ~shares)


async def due_payments(
    session: AsyncSession,
    *,
    shop_id: int | None,
    rule: ApprovalRule,
    now: datetime | None = None,
    limit: int = BATCH_LIMIT,
) -> list[Payment]:
    """Oldest-first receipts of one shop that waited long enough and pass every safety condition."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=rule.minutes)
    conditions = [
        _waiting_clause(),
        _shop_clause(shop_id),
        Payment.receipt_at <= cutoff,
        BotUser.is_blocked.is_(False),
        _is_unique_receipt(Payment),
    ]
    if rule.max_amount:
        conditions.append(Payment.amount <= rule.max_amount)
    if rule.returning_only:
        conditions.append(_returning_customer(Order, shop_id))
    rows = await session.scalars(
        select(Payment)
        .join(Order, Order.id == Payment.order_id)
        .join(BotUser, BotUser.id == Payment.user_id)
        .where(*conditions)
        .order_by(Payment.receipt_at.asc(), Payment.id.asc())
        .limit(max(1, min(int(limit), BATCH_LIMIT)))
    )
    return list(rows.all())


SettingsLoader = Callable[[int | None], Awaitable[Mapping[str, str]]]
BotOpener = Callable[[int | None], Awaitable[tuple[Bot | None, bool]]]


async def _stop_retrying(session: AsyncSession, payment_id: int) -> None:
    """A receipt the automatic attempt could not approve is left for a person, not retried every minute."""
    await session.execute(
        update(Payment)
        .where(Payment.id == int(payment_id), Payment.status == PaymentStatus.PENDING.value)
        .values(receipt_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _approve_one(session: AsyncSession, payment_id: int, bot: Bot, rule: ApprovalRule) -> bool:
    """Approve one due receipt unless a person decided it meanwhile; True when it was approved here."""
    payment = await session.get(Payment, payment_id, populate_existing=True)
    if payment is None or payment.status != PaymentStatus.PENDING.value:
        return False
    user = await session.get(BotUser, payment.user_id)
    await auto_approve_receipt(
        session, payment, bot=bot, user_tg_id=int(user.telegram_id) if user else None, reason=rule.reason
    )
    await session.refresh(payment)
    if payment.status == PaymentStatus.PENDING.value:
        await _stop_retrying(session, payment_id)
        return False
    return True


async def approve_due_receipts(
    session: AsyncSession,
    *,
    settings_for: SettingsLoader,
    open_bot: BotOpener,
    now: datetime | None = None,
) -> int:
    """One pass over every shop with waiting receipts; returns how many were approved.

    Failures are caught per receipt and per shop so one bad row never stops the batch.
    """
    approved = 0
    for shop_id in await shops_with_waiting_receipts(session):
        try:
            rule = rule_from_settings(await settings_for(shop_id))
            if rule is None:
                continue
            due_ids = [p.id for p in await due_payments(session, shop_id=shop_id, rule=rule, now=now)]
            if not due_ids:
                continue
            bot, should_close = await open_bot(shop_id)
            if bot is None:
                logger.warning("scheduled approval: no bot for shop=%s", shop_id)
                continue
            try:
                for payment_id in due_ids:
                    try:
                        approved += int(await _approve_one(session, int(payment_id), bot, rule))
                    except Exception:
                        logger.exception("scheduled approval failed payment=%s", payment_id)
                        await rollback_quiet(session)
                        await _stop_retrying(session, int(payment_id))
            finally:
                if should_close:
                    await bot.session.close()
        except Exception:
            logger.exception("scheduled approval failed for shop=%s", shop_id)
            await rollback_quiet(session)
    return approved
