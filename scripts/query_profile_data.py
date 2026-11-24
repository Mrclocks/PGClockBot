#!/usr/bin/env python3
"""Extra sample rows so the query profile can tell a constant cost from one that grows with data.

A page or button that runs one query per row looks cheap with three rows. The profile is therefore
run twice: once on the small sample data and once with ``scale`` more customers, orders, payments,
services, tickets and wallet entries. A unit of work whose statement count grows between the two
runs is doing work per row. All ids start at ``FIRST_ID`` so they never meet the sample data.

    DATABASE_URL=postgresql+asyncpg://... python scripts/query_profile_data.py SCALE [PERSON_USER_ID]

adds the rows to an already prepared database (``scripts/ui_fixture.py``); ``PERSON_USER_ID`` gets
its own share of services, orders, tickets and wallet entries, as a busy customer would.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIRST_ID = 100_000
EPOCH = datetime(2026, 1, 10, 9, 30, tzinfo=timezone.utc)
GB = 1024**3
ORDER_STATUSES = ("delivered", "awaiting_approval", "rejected", "paid")
TABLES = ("bot_users", "orders", "payments", "user_services", "tickets", "ticket_messages", "wallet_transactions")


def _at(offset: int) -> datetime:
    return EPOCH - timedelta(hours=offset)


def person_rows(user_id: int, scale: int, *, plan_id: int, first_id: int) -> list[object]:
    """Services, orders with payments, tickets and wallet entries of one customer."""
    from app.db.models import Order, Payment, Ticket, TicketMessage, UserService, WalletTransaction

    rows: list[object] = []
    for n in range(scale):
        row_id = first_id + n
        status = ORDER_STATUSES[n % len(ORDER_STATUSES)]
        rows += [
            UserService(id=row_id, bot_user_id=user_id, plan_id=plan_id, pg_user_id=row_id,
                        pg_username=f"profile_{row_id}", shop_needs_review=False, created_at=_at(n),
                        quota_data_limit_bytes=30 * GB, quota_expire_at=EPOCH + timedelta(days=10 + n % 20)),
            Order(id=row_id, user_id=user_id, plan_id=plan_id, amount=150_000, status=status,
                  created_at=_at(n), updated_at=_at(n)),
            Payment(id=row_id, order_id=row_id, user_id=user_id, amount=150_000, method="card",
                    status="approved" if status in ("delivered", "paid") else "pending", created_at=_at(n)),
            Ticket(id=row_id, user_id=user_id, subject=f"پرسش {n}", status="open" if n % 2 else "closed",
                   created_at=_at(n), updated_at=_at(n)),
            TicketMessage(id=row_id, ticket_id=row_id, sender_id=user_id, body="سلام", created_at=_at(n)),
            WalletTransaction(id=row_id, user_id=user_id, amount=10_000, balance_after=10_000 * (n + 1),
                              reason="profile", created_at=_at(n)),
        ]
    return rows


def bulk_rows(scale: int, *, plan_id: int) -> list[object]:
    """``scale`` more customers, each with one service, one order and payment and one ticket."""
    from app.db.models import BotUser

    rows: list[object] = []
    for n in range(scale):
        user_id = FIRST_ID + n
        rows.append(BotUser(id=user_id, telegram_id=7_200_000_000 + n, username=f"profile{n}",
                            full_name=f"مشتری حجمی {n}", role="user", referral_code=f"QP{n:06d}",
                            wallet_balance=n * 1_000, created_at=_at(n)))
    for n in range(scale):
        rows += person_rows(FIRST_ID + n, 1, plan_id=plan_id, first_id=FIRST_ID + 2 * scale + n)
    return rows


async def seed(scale: int, person_user_id: int | None) -> None:
    from sqlalchemy import select, text

    from app.db.models import Plan
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        plan_id = await session.scalar(select(Plan.id).order_by(Plan.id).limit(1))
        if plan_id is None:
            raise SystemExit("query-profile data: the database has no plan; prepare it with ui_fixture.py first")
        session.add_all(bulk_rows(scale, plan_id=plan_id))
        await session.flush()
        if person_user_id is not None:
            session.add_all(person_rows(person_user_id, scale, plan_id=plan_id, first_id=FIRST_ID + 4 * scale))
        await session.commit()
        for table in TABLES:  # fixed names from the tuple above
            await session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"
            ))
        await session.commit()


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-4].strip(), file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path.cwd()))
    scale = int(sys.argv[1])
    person = int(sys.argv[2]) if len(sys.argv) == 3 else None
    asyncio.run(seed(scale, person))
    print(f"query-profile data: {scale} more customers" + (f", {scale} rows each for user {person}" if person else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
