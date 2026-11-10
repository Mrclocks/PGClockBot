"""Monthly credit statements, debt payment and the overdue ladder of PAYG shops.

On the first run after a calendar month ends (UTC), each shop with a credit line gets one
statement for that month: the debt at close is due ``due_days`` later. A statement is paid
when the shop's balance plus its PAYG spending since the close is no longer negative, so
payments clear the oldest debt first without an allocation table.

While a statement is unpaid past its due date the shop climbs the ladder (days after due,
from settings): overdue (the line lends nothing new), no new users, no renewals or other
changes, then blocked (the line no longer counts and the empty-wallet suspension acts).
Paying lifts every step at once. Each step is announced once per statement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    CreditStatement,
    ResellerBillingTransaction,
    ResellerProfile,
    ShopCreditAccount,
    WalletTransaction,
)
from app.py_compat import StrEnum
from app.services.billing import (
    KIND_CHARGE,
    KIND_CHARGE_REFUND,
    KIND_USAGE,
    record_payg_ledger_entry,
)
from app.services.payg_pricing import ChargeEvent
from app.services.shop_credit import CreditStatus
from app.services.users import get_setting

logger = logging.getLogger(__name__)

KIND_WRITEOFF = "writeoff"
SPENDING_KINDS = (KIND_CHARGE, KIND_CHARGE_REFUND, KIND_USAGE)
ACCOUNT_LIMIT = 1_000
SETTING_DEFAULTS = {
    "credit_due_days": 7,
    "credit_block_create_days": 3,
    "credit_block_changes_days": 7,
    "credit_block_days": 14,
}
RESTRICTED_CREATE = "به دلیل بدهی سررسیدگذشته ساخت کاربر تازه بسته است؛ صورت‌حساب را پرداخت کنید"
RESTRICTED_CHANGES = "به دلیل بدهی سررسیدگذشته تمدید و تغییر کاربر بسته است؛ صورت‌حساب را پرداخت کنید"


class StatementStatus(StrEnum):
    ISSUED = "issued"
    PAID = "paid"
    WRITTEN_OFF = "written_off"


class LadderStep(IntEnum):
    NONE = 0
    OVERDUE = 1
    NO_CREATE = 2
    NO_CHANGES = 3
    BLOCKED = 4


# notified_step 1 is the issue notice; ladder step n is notice n + 1.
ISSUE_NOTICE = 1


@dataclass(frozen=True, slots=True)
class LadderDays:
    due: int
    block_create: int
    block_changes: int
    block: int


def previous_month(now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` of the calendar month before ``now`` (UTC)."""
    this_start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_start = (this_start - timedelta(days=1)).replace(day=1)
    return last_start, this_start


def ladder_step(*, now: datetime, due_at: datetime, days: LadderDays) -> LadderStep:
    if now < due_at:
        return LadderStep.NONE
    late = now - due_at
    if late >= timedelta(days=days.block):
        return LadderStep.BLOCKED
    if late >= timedelta(days=days.block_changes):
        return LadderStep.NO_CHANGES
    if late >= timedelta(days=days.block_create):
        return LadderStep.NO_CREATE
    return LadderStep.OVERDUE


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _days_setting(session: AsyncSession, key: str) -> int:
    raw = await get_setting(session, key, str(SETTING_DEFAULTS[key]))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return SETTING_DEFAULTS[key]
    return value if 0 <= value <= 365 else SETTING_DEFAULTS[key]


async def ladder_days(session: AsyncSession) -> LadderDays:
    return LadderDays(
        due=await _days_setting(session, "credit_due_days"),
        block_create=await _days_setting(session, "credit_block_create_days"),
        block_changes=await _days_setting(session, "credit_block_changes_days"),
        block=await _days_setting(session, "credit_block_days"),
    )


async def _ledger_sum(session: AsyncSession, shop_id: int, kinds: tuple[str, ...], *, start: datetime,
                      end: datetime | None = None) -> int:
    query = select(func.coalesce(func.sum(ResellerBillingTransaction.amount), 0)).where(
        ResellerBillingTransaction.reseller_user_id == int(shop_id),
        ResellerBillingTransaction.kind.in_(kinds),
        ResellerBillingTransaction.created_at >= start,
    )
    if end is not None:
        query = query.where(ResellerBillingTransaction.created_at < end)
    return int(await session.scalar(query) or 0)


async def spending_since(session: AsyncSession, shop_id: int, since: datetime) -> int:
    """PAYG spending (charges and usage, net of refunds) after ``since``, as a positive number."""
    return max(0, -await _ledger_sum(session, shop_id, SPENDING_KINDS, start=since))


async def _wallet_moves(session: AsyncSession, shop_id: int, *, start: datetime, end: datetime | None = None,
                        positive_only: bool = False) -> int:
    """Sum of every movement of the shop's own wallet: the PAYG ledger plus the wallet ledger."""
    payg = select(func.coalesce(func.sum(ResellerBillingTransaction.amount), 0)).where(
        ResellerBillingTransaction.reseller_user_id == int(shop_id), ResellerBillingTransaction.created_at >= start,
    )
    wallet = select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
        WalletTransaction.user_id == int(shop_id), WalletTransaction.reseller_id.is_(None),
        WalletTransaction.created_at >= start,
    )
    if end is not None:
        payg = payg.where(ResellerBillingTransaction.created_at < end)
        wallet = wallet.where(WalletTransaction.created_at < end)
    if positive_only:
        payg = payg.where(ResellerBillingTransaction.amount > 0)
        wallet = wallet.where(WalletTransaction.amount > 0)
    return int(await session.scalar(payg) or 0) + int(await session.scalar(wallet) or 0)


async def _balance(session: AsyncSession, shop_id: int) -> int:
    return int(await session.scalar(select(BotUser.wallet_balance).where(BotUser.id == int(shop_id))) or 0)


async def close_month(session: AsyncSession, shop_id: int, *, now: datetime, days: LadderDays) -> CreditStatement | None:
    """The statement of last month for this shop, created once; ``None`` if it already existed."""
    start, end = previous_month(now)
    existing = await session.scalar(
        select(CreditStatement.id).where(CreditStatement.shop_id == int(shop_id), CreditStatement.period_start == start)
    )
    if existing is not None:
        return None
    # The balance at close is the balance now without every wallet movement after the month ended.
    balance_at_close = await _balance(session, shop_id) - await _wallet_moves(session, shop_id, start=end)
    debt_at_close = max(0, -balance_at_close)
    statement = CreditStatement(
        shop_id=int(shop_id), period_start=start, period_end=end, amount_due=debt_at_close,
        charges_total=max(0, -await _ledger_sum(session, shop_id, SPENDING_KINDS, start=start, end=end)),
        payments_total=await _wallet_moves(session, shop_id, start=start, end=end, positive_only=True),
        due_at=end + timedelta(days=days.due),
        status=StatementStatus.PAID if debt_at_close == 0 else StatementStatus.ISSUED,
        resolved_at=now if debt_at_close == 0 else None,
    )
    try:
        async with session.begin_nested():
            session.add(statement)
            await session.flush()
    except IntegrityError:
        return None
    await session.commit()
    return statement


async def is_statement_paid(session: AsyncSession, statement: CreditStatement) -> bool:
    balance = await _balance(session, int(statement.shop_id))
    return balance + await spending_since(session, int(statement.shop_id), _aware(statement.period_end)) >= 0


async def resolve_statement(
    session: AsyncSession, statement: CreditStatement, status: StatementStatus, *, now: datetime
) -> bool:
    """Move an issued statement to ``status`` once; False when it was already resolved."""
    result = await session.execute(
        update(CreditStatement)
        .where(CreditStatement.id == int(statement.id), CreditStatement.status == str(StatementStatus.ISSUED))
        .values(status=str(status), resolved_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def open_statements(session: AsyncSession, shop_id: int) -> list[CreditStatement]:
    return list((await session.scalars(
        select(CreditStatement)
        .where(CreditStatement.shop_id == int(shop_id), CreditStatement.status == str(StatementStatus.ISSUED))
        .order_by(CreditStatement.period_start)
        .limit(36)
    )).all())


async def claim_notice(session: AsyncSession, statement: CreditStatement, notice: int) -> bool:
    result = await session.execute(
        update(CreditStatement)
        .where(CreditStatement.id == int(statement.id), CreditStatement.notified_step < int(notice))
        .values(notified_step=int(notice))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def write_off_statement(session: AsyncSession, statement_id: int, *, actor: str, now: datetime) -> bool:
    """Forgive what is still unpaid of a statement, credited back once. Owner only (caller checks)."""
    statement = await session.get(CreditStatement, int(statement_id))
    if statement is None or statement.status != StatementStatus.ISSUED:
        return False
    shop_id = int(statement.shop_id)
    unpaid = max(0, -(await _balance(session, shop_id) + await spending_since(session, shop_id, _aware(statement.period_end))))
    forgiven = min(int(statement.amount_due), unpaid)
    if not await resolve_statement(session, statement, StatementStatus.WRITTEN_OFF, now=now):
        return False
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == shop_id))
    if profile is not None and forgiven > 0:
        await record_payg_ledger_entry(
            session, profile, kind=KIND_WRITEOFF, amount=forgiven,
            idempotency_key=f"writeoff:{int(statement.id)}", note="بخشودگی صورت‌حساب", created_by=actor,
        )
    await session.commit()
    return True


def refusal_for(restriction: int, event: ChargeEvent) -> str | None:
    """Why the ladder refuses ``event`` now, or ``None`` when it is allowed."""
    if restriction >= LadderStep.NO_CHANGES:
        return RESTRICTED_CREATE if event == ChargeEvent.CREATE else RESTRICTED_CHANGES
    if restriction >= LadderStep.NO_CREATE and event == ChargeEvent.CREATE:
        return RESTRICTED_CREATE
    return None


async def shop_restriction(session: AsyncSession, shop_id: int) -> int:
    value = await session.scalar(select(ShopCreditAccount.restriction).where(ShopCreditAccount.shop_id == int(shop_id)))
    return int(value or 0)


async def accounts_to_cycle(session: AsyncSession) -> list[ShopCreditAccount]:
    """Shops with a credit line or a step in force; bounded."""
    return list((await session.scalars(
        select(ShopCreditAccount)
        .where((ShopCreditAccount.credit_limit > 0) | (ShopCreditAccount.restriction > 0)
               | (ShopCreditAccount.temp_limit.is_not(None)))
        .order_by(ShopCreditAccount.id)
        .limit(ACCOUNT_LIMIT)
    )).all())


def account_status_for(step: LadderStep, current: str) -> str:
    """Line status a ladder step implies; a freeze set by the owner is kept once paid."""
    if step >= LadderStep.BLOCKED:
        return str(CreditStatus.BLOCKED)
    if step >= LadderStep.OVERDUE:
        return str(CreditStatus.OVERDUE)
    if current in (CreditStatus.OVERDUE, CreditStatus.BLOCKED):
        return str(CreditStatus.ACTIVE)
    return current
