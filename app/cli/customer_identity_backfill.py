"""Run after schema upgrade: python -m app.cli.customer_identity_backfill."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SessionLocal
from app.services.customer_backfill import backfill_legacy_customers, backfill_legacy_services


async def run_backfill(sessions: async_sessionmaker[AsyncSession], *, users_after: int, services_after: int) -> None:
    for label, operation, cursor in (
        ("users", backfill_legacy_customers, users_after), ("services", backfill_legacy_services, services_after),
    ):
        while True:
            async with sessions() as session:
                result = await operation(session, after=cursor)
                await session.commit()
            cursor = result.last_id
            print(f"{label}: committed through {cursor}, scanned {result.scanned}", flush=True)
            if not result.scanned:
                break


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill canonical legacy identities in committed batches of 500")
    parser.add_argument("--users-after", type=int, default=0)
    parser.add_argument("--services-after", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run_backfill(SessionLocal, users_after=args.users_after, services_after=args.services_after))


if __name__ == "__main__":
    main()
