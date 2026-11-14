"""Lift a shop's overdue restriction as soon as a payment settles its statements.

The hourly ``credit_cycle`` run would lift it too; this only shortens the wait. A wallet
credit marks the shop on the session (``mark_restricted_shop_paid``) inside the credit's
own transaction; once that transaction commits, a background task runs the same
``cycle_shop`` step for that shop in its own session. Nothing runs when the credit rolls
back, and a failure is logged and left to the hourly run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import ShopCreditAccount
from app.services.credit_cycle import cycle_shop
from app.services.credit_statements import LadderStep, ladder_days
from app.services.db_safe import rollback_quiet

logger = logging.getLogger(__name__)

_PENDING_KEY = "credit_lift_shop_ids"
_tasks: set[asyncio.Task] = set()


async def mark_restricted_shop_paid(session: AsyncSession, shop_id: int) -> None:
    """Inside a wallet credit: lift the shop's restriction after this transaction commits."""
    with session.no_autoflush:
        restriction = await session.scalar(
            select(ShopCreditAccount.restriction).where(ShopCreditAccount.shop_id == int(shop_id))
        )
    if int(restriction or 0) > LadderStep.NONE:
        session.info.setdefault(_PENDING_KEY, set()).add(int(shop_id))


async def lift_paid_restriction(session: AsyncSession, shop_id: int, *, now: datetime | None = None) -> int:
    """Re-run the ladder for one shop; returns its restriction afterwards."""
    account = await session.scalar(select(ShopCreditAccount).where(ShopCreditAccount.shop_id == int(shop_id)))
    if account is None:
        return int(LadderStep.NONE)
    if int(account.restriction or 0) > LadderStep.NONE:
        await cycle_shop(session, account, now=now or datetime.now(timezone.utc), days=await ladder_days(session))
    return int(account.restriction or 0)


async def _lift_in_own_session(shop_id: int) -> None:
    # Local import: the app session module builds the engine at import time.
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        try:
            await lift_paid_restriction(session, shop_id)
        except Exception:  # noqa: BLE001 - best effort after a committed payment; the hourly run retries
            logger.warning("credit lift after payment failed shop=%s", shop_id, exc_info=True)
            await rollback_quiet(session)


def schedule_lift(shop_id: int) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync tooling): the hourly run lifts it
    task = loop.create_task(_lift_in_own_session(int(shop_id)), name="credit-lift")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@event.listens_for(Session, "after_commit")
def _lift_after_commit(session: Session) -> None:
    for shop_id in session.info.pop(_PENDING_KEY, ()):
        schedule_lift(shop_id)


@event.listens_for(Session, "after_rollback")
def _forget_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)
