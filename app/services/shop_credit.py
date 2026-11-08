"""Credit lines of PAYG shops: how far a shop's wallet may go below zero for PAYG charges.

Only the platform owner sets a shop's limit. Spending still goes through
``billing.record_payg_ledger_entry``; this module answers how much credit a shop has
now, records every change in ``CreditEvent``, and sends the «credit used» alerts at
70%, 90% and 100% once per climb (a top-up below 70% re-arms them).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CreditEvent, ShopCreditAccount
from app.py_compat import StrEnum

logger = logging.getLogger(__name__)

MAX_CREDIT = 2_000_000_000
ALERT_LEVELS: tuple[int, ...] = (70, 90, 100)


class CreditStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    OVERDUE = "overdue"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CreditUsage:
    limit: int
    used: int

    @property
    def available(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def percent(self) -> int:
        return 0 if self.limit <= 0 else min(100, (self.used * 100) // self.limit)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def effective_limit(account: ShopCreditAccount | None, *, now: datetime) -> int:
    """Credit the shop may use now; an account that is not active lends nothing."""
    if account is None or account.status != CreditStatus.ACTIVE:
        return 0
    limit = max(0, int(account.credit_limit or 0))
    until = _aware(account.temp_limit_until)
    if account.temp_limit is not None and until is not None and until > now:
        limit = max(limit, int(account.temp_limit))
    return limit


async def credit_account(session: AsyncSession, shop_id: int) -> ShopCreditAccount | None:
    return await session.scalar(select(ShopCreditAccount).where(ShopCreditAccount.shop_id == int(shop_id)))


async def shop_credit_limit(session: AsyncSession, shop_id: int, *, now: datetime | None = None) -> int:
    return effective_limit(await credit_account(session, shop_id), now=now or datetime.now(timezone.utc))


def credit_usage(balance: int, limit: int) -> CreditUsage:
    return CreditUsage(limit=max(0, int(limit)), used=max(0, -int(balance)))


def _checked_amount(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_CREDIT:
        raise ValueError(f"«{label}» نامعتبر است")
    return value


async def _account_for_update(session: AsyncSession, shop_id: int) -> ShopCreditAccount:
    account = await credit_account(session, shop_id)
    if account is not None:
        return account
    try:
        async with session.begin_nested():
            session.add(ShopCreditAccount(shop_id=int(shop_id)))
            await session.flush()
    except IntegrityError:
        logger.debug("credit account created concurrently shop=%s", shop_id)
    account = await credit_account(session, shop_id)
    if account is None:
        raise ValueError("ساخت حساب اعتبار ناموفق بود")
    return account


def _event(shop_id: int, kind: str, old: object, new: object, *, actor: str, note: str = "") -> CreditEvent:
    return CreditEvent(
        shop_id=int(shop_id), kind=kind, old_value=None if old is None else str(old)[:64],
        new_value=None if new is None else str(new)[:64], actor=(actor or "")[:128] or None,
        note=(note or "")[:255] or None,
    )


@dataclass(frozen=True, slots=True)
class CreditLimitChange:
    limit: int
    temp_limit: int | None = None
    temp_limit_until: datetime | None = None
    note: str = ""


async def set_credit_limit(
    session: AsyncSession, shop_id: int, change: CreditLimitChange, *, actor: str
) -> ShopCreditAccount:
    """Set a shop's credit line (owner only, checked by the caller) and record it."""
    limit = _checked_amount(change.limit, label="سقف اعتبار")
    temp = None if change.temp_limit is None else _checked_amount(change.temp_limit, label="سقف موقت")
    until = _aware(change.temp_limit_until)
    if temp is not None and (until is None or until <= datetime.now(timezone.utc)):
        raise ValueError("برای سقف موقت تاریخ پایان آینده لازم است")
    account = await _account_for_update(session, shop_id)
    events = []
    if int(account.credit_limit or 0) != limit:
        events.append(_event(shop_id, "limit", account.credit_limit, limit, actor=actor, note=change.note))
    if account.temp_limit != temp or _aware(account.temp_limit_until) != (until if temp is not None else None):
        events.append(_event(shop_id, "temp_limit", account.temp_limit, temp, actor=actor, note=change.note))
    account.credit_limit = limit
    account.temp_limit = temp
    account.temp_limit_until = until if temp is not None else None
    session.add_all(events)
    await session.commit()
    return account


async def set_credit_status(
    session: AsyncSession, shop_id: int, status: CreditStatus, *, actor: str, note: str = ""
) -> ShopCreditAccount:
    """Freeze or reopen a credit line; a frozen line lends nothing new."""
    account = await _account_for_update(session, shop_id)
    if account.status != status:
        session.add(_event(shop_id, "status", account.status, str(status), actor=actor, note=note))
        account.status = str(status)
        await session.commit()
    return account


def _alert_level_for(percent: int) -> int:
    reached = [level for level in ALERT_LEVELS if percent >= level]
    return max(reached) if reached else 0


async def claim_credit_alert(session: AsyncSession, shop_id: int, *, balance: int, limit: int) -> int | None:
    """The alert level to send now, claimed so it is sent once; re-arms below 70%.

    Returns ``None`` when nothing new is due.
    """
    level = _alert_level_for(credit_usage(balance, limit).percent)
    if level == 0:
        await session.execute(
            update(ShopCreditAccount)
            .where(ShopCreditAccount.shop_id == int(shop_id), ShopCreditAccount.alert_level > 0)
            .values(alert_level=0)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return None
    result = await session.execute(
        update(ShopCreditAccount)
        .where(ShopCreditAccount.shop_id == int(shop_id), ShopCreditAccount.alert_level < level)
        .values(alert_level=level)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return level if result.rowcount == 1 else None


async def recent_credit_events(session: AsyncSession, shop_id: int, *, limit: int = 20) -> list[CreditEvent]:
    return list(
        (
            await session.scalars(
                select(CreditEvent)
                .where(CreditEvent.shop_id == int(shop_id))
                .order_by(CreditEvent.id.desc())
                .limit(max(1, min(int(limit), 100)))
            )
        ).all()
    )


@dataclass(frozen=True, slots=True)
class CreditOverview:
    account: ShopCreditAccount | None
    usage: CreditUsage
    balance: int
    events: tuple[CreditEvent, ...]

    @property
    def status(self) -> str:
        return str(self.account.status) if self.account is not None else str(CreditStatus.ACTIVE)


async def credit_overview(session: AsyncSession, shop_id: int, *, balance: int) -> CreditOverview:
    """What the owner sees on the reseller card: line, use, recent changes."""
    account = await credit_account(session, shop_id)
    limit = effective_limit(account, now=datetime.now(timezone.utc))
    events = tuple(await recent_credit_events(session, shop_id, limit=10))
    return CreditOverview(account=account, usage=credit_usage(balance, limit), balance=int(balance), events=events)
