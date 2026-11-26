"""«وقتی استارت زد خبرم کن»: remember whom a staff member is waiting for, and find them once they arrive.

Used when the owner wants to make someone a reseller who has not started the bot yet. The watch only
leads to a message to the watcher with the person's numeric id; the make-reseller step is still the
owner's explicit choice, so a username that moved to another account grants nothing by itself.
Watches end after ``WATCH_DAYS``. ``claim_start_watch`` is an atomic claim, so a watch is announced
at most once even when two job runs overlap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, StartWatch
from app.services.telegram_target import TelegramTarget

WATCH_DAYS = 7
BATCH_LIMIT = 100
PURGE_LIMIT = 500
_ID_PREFIX = "id:"
_USERNAME_PREFIX = "u:"


def target_key(target: TelegramTarget) -> str:
    """``id:<n>`` or ``u:<lower-case username>``: the stored form of whom the watch is for."""
    if target.telegram_id is not None:
        return f"{_ID_PREFIX}{int(target.telegram_id)}"
    return f"{_USERNAME_PREFIX}{str(target.username or '').lower()}"


async def add_start_watch(
    session: AsyncSession, *, watcher_telegram_id: int, target: TelegramTarget, now: datetime | None = None
) -> bool:
    """Open a watch; ``False`` when the same watcher already waits for the same person."""
    moment = now or datetime.now(timezone.utc)
    stmt = (
        insert(StartWatch)
        .values(
            watcher_telegram_id=int(watcher_telegram_id),
            target=target_key(target),
            created_at=moment,
            expires_at=moment + timedelta(days=WATCH_DAYS),
        )
        .on_conflict_do_nothing(
            index_elements=["watcher_telegram_id", "target"], index_where=StartWatch.notified_at.is_(None)
        )
    )
    result = await session.execute(stmt)
    await session.commit()
    return bool(result.rowcount)


def _matches(watch: StartWatch, by_id: dict[int, BotUser], by_name: dict[str, BotUser]) -> BotUser | None:
    if watch.target.startswith(_ID_PREFIX):
        return by_id.get(int(watch.target.removeprefix(_ID_PREFIX)))
    return by_name.get(watch.target.removeprefix(_USERNAME_PREFIX))


async def arrived_start_watches(session: AsyncSession, *, now: datetime) -> list[tuple[StartWatch, BotUser]]:
    """Open, unexpired watches whose person has started the bot, with that person; two queries in all."""
    watches = list(
        await session.scalars(
            select(StartWatch)
            .where(StartWatch.notified_at.is_(None), StartWatch.expires_at > now)
            .order_by(StartWatch.id)
            .limit(BATCH_LIMIT)
        )
    )
    ids = {int(w.target.removeprefix(_ID_PREFIX)) for w in watches if w.target.startswith(_ID_PREFIX)}
    names = {w.target.removeprefix(_USERNAME_PREFIX) for w in watches if w.target.startswith(_USERNAME_PREFIX)}
    if not ids and not names:
        return []
    users = list(
        await session.scalars(
            select(BotUser)
            .where(or_(BotUser.telegram_id.in_(ids), func.lower(BotUser.username).in_(names)))
            .order_by(BotUser.id)
            .limit(BATCH_LIMIT * 2)
        )
    )
    by_id = {int(u.telegram_id): u for u in users}
    by_name: dict[str, BotUser] = {}
    ambiguous: set[str] = set()
    for user in users:
        name = (user.username or "").lower()
        if name not in names:
            continue
        if name in by_name:
            ambiguous.add(name)
        else:
            by_name[name] = user
    # Two stored people with one username: wait for the watcher to name them by id instead.
    for name in ambiguous:
        by_name.pop(name, None)
    return [(w, user) for w in watches if (user := _matches(w, by_id, by_name)) is not None]


async def claim_start_watch(session: AsyncSession, watch_id: int, *, now: datetime) -> bool:
    """Mark the watch announced; ``False`` when another run already did."""
    result = await session.execute(
        update(StartWatch)
        .where(StartWatch.id == int(watch_id), StartWatch.notified_at.is_(None))
        .values(notified_at=now)
    )
    await session.commit()
    return result.rowcount == 1


async def purge_start_watches(session: AsyncSession, *, now: datetime) -> int:
    """Drop expired watches and announced ones older than ``WATCH_DAYS``, a bounded batch at a time."""
    old = now - timedelta(days=WATCH_DAYS)
    doomed = (
        select(StartWatch.id)
        .where(or_(StartWatch.expires_at <= now, StartWatch.notified_at <= old))
        .limit(PURGE_LIMIT)
        .scalar_subquery()
    )
    result = await session.execute(delete(StartWatch).where(StartWatch.id.in_(doomed)))
    await session.commit()
    return int(result.rowcount or 0)
