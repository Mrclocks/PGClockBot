"""Daily run of the credit statements: close last month, settle paid debt, apply the ladder.

Orchestration of ``credit_statements`` for every shop with a credit line: one shop's
failure is logged and the run continues. Notices are claimed before they are sent and
sent only after the change that caused them is committed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CreditEvent, ResellerProfile, ShopCreditAccount
from app.services.billing_alerts import notify_statement
from app.services.billing_suspend import restore_payg_reseller, suspend_payg_reseller
from app.services.credit_statements import (
    ISSUE_NOTICE,
    LadderDays,
    LadderStep,
    StatementStatus,
    resolve_statement,
    accounts_to_cycle,
    account_status_for,
    claim_notice,
    close_month,
    is_statement_paid,
    ladder_days,
    ladder_step,
    open_statements,
)
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)


async def _notice(session: AsyncSession, profile: ResellerProfile, statement, notice: int) -> None:
    if await claim_notice(session, statement, notice):
        due = statement.due_at.strftime("%Y-%m-%d") if statement.due_at else ""
        await notify_statement(session, profile, notice=notice, amount_due=int(statement.amount_due), due_text=due)


async def _ladder_for_shop(session: AsyncSession, profile: ResellerProfile, *, now: datetime,
                           days: LadderDays) -> LadderStep:
    step = LadderStep.NONE
    for statement in await open_statements(session, int(profile.user_id)):
        if await is_statement_paid(session, statement):
            await resolve_statement(session, statement, StatementStatus.PAID, now=now)
            await session.commit()
            continue
        due = statement.due_at if statement.due_at.tzinfo else statement.due_at.replace(tzinfo=timezone.utc)
        current = ladder_step(now=now, due_at=due, days=days)
        step = max(step, current)
        if current > LadderStep.NONE:
            await _notice(session, profile, statement, int(current) + ISSUE_NOTICE)
    return step


async def apply_step(session: AsyncSession, account: ShopCreditAccount, profile: ResellerProfile,
                     step: LadderStep) -> None:
    """Store the step and the line status it implies; block or unblock the shop on the last step."""
    previous = LadderStep(int(account.restriction or 0))
    status = account_status_for(step, str(account.status))
    if status != account.status:
        session.add(CreditEvent(shop_id=int(account.shop_id), kind="status", old_value=account.status,
                                new_value=status, actor="ladder"))
    account.restriction = int(step)
    account.status = status
    await session.commit()
    if step >= LadderStep.BLOCKED > previous:
        await suspend_payg_reseller(session, profile, commit=True)
    elif previous >= LadderStep.BLOCKED > step and profile.billing_suspended_at is not None:
        await restore_payg_reseller(session, profile, commit=True)


async def cycle_shop(session: AsyncSession, account: ShopCreditAccount, *, now: datetime, days: LadderDays) -> None:
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(account.shop_id)))
    if profile is None:
        return
    statement = await close_month(session, int(account.shop_id), now=now, days=days)
    if statement is not None and int(statement.amount_due) > 0:
        await _notice(session, profile, statement, ISSUE_NOTICE)
    step = await _ladder_for_shop(session, profile, now=now, days=days)
    await apply_step(session, account, profile, step)


async def run_credit_cycle(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    days = await ladder_days(session)
    stats = {"shops": 0, "errors": 0}
    for account in await accounts_to_cycle(session):
        stats["shops"] += 1
        try:
            await cycle_shop(session, account, now=now, days=days)
        except Exception:  # noqa: BLE001 - per-shop isolation in a background job; logged and counted
            stats["errors"] += 1
            logger.warning("credit cycle failed shop=%s", account.shop_id, exc_info=True)
            await rollback_quiet(session)
    return stats
