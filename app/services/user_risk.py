"""Risk flags on a customer (new account, rejected receipts) and their labels."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Payment,
    PaymentStatus,
    Plan,
    Ticket,
)

RISK_FLAG_LABELS = {
    "multi_trial": "چند تست",
    "repeat_reject": "رد مکرر",
    "many_tickets": "تیکت زیاد",
    "manual": "دستی",
}


def parse_risk_flags(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for part in str(raw).replace(";", ",").split(","):
        key = part.strip().lower()
        if key and key not in out:
            out.append(key)
    return out


def serialize_risk_flags(flags: list[str] | None) -> str | None:
    cleaned = parse_risk_flags(",".join(flags or []))
    return ",".join(cleaned) if cleaned else None


def risk_badge_label(flags: list[str] | None) -> str | None:
    if not flags:
        return None
    labels = [RISK_FLAG_LABELS.get(f, f) for f in flags]
    return " · ".join(labels)


def compute_user_risk_flags(
    *,
    trial_count: int = 0,
    rejected_payments: int = 0,
    open_tickets: int = 0,
    existing: list[str] | None = None,
) -> list[str]:
    flags = list(existing or [])
    if trial_count >= 2 and "multi_trial" not in flags:
        flags.append("multi_trial")
    if rejected_payments >= 3 and "repeat_reject" not in flags:
        flags.append("repeat_reject")
    if open_tickets >= 3 and "many_tickets" not in flags:
        flags.append("many_tickets")
    return flags


async def refresh_user_risk(session: AsyncSession, user: BotUser) -> list[str]:
    from app.db.models import Order as Ord

    trial_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Ord)
                .join(Plan, Plan.id == Ord.plan_id)
                .where(Ord.user_id == user.id, Plan.is_trial.is_(True))
            )
        ).scalar()
        or 0
    )
    rejected = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Payment)
                .where(
                    Payment.user_id == user.id,
                    Payment.status == PaymentStatus.REJECTED.value,
                )
            )
        ).scalar()
        or 0
    )
    tickets = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.user_id == user.id, Ticket.status == "open")
            )
        ).scalar()
        or 0
    )
    existing = [f for f in parse_risk_flags(user.risk_flags) if f == "manual"]
    flags = compute_user_risk_flags(
        trial_count=trial_count,
        rejected_payments=rejected,
        open_tickets=tickets,
        existing=existing,
    )
    user.risk_flags = serialize_risk_flags(flags)
    await session.flush()
    return flags
