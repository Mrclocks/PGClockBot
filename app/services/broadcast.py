from __future__ import annotations

"""Admin broadcast / bulk messaging to bot users."""

import asyncio
import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, BroadcastLog, Role
from app.services.shop_membership import shop_member_clause

logger = logging.getLogger("pgclock.broadcast")

AUDIENCE_LABELS = {
    "all": "همه",
    "users": "کاربران عادی",
    "resellers": "نمایندگان",
    "admins": "ادمین‌ها",
}


async def list_broadcast_targets(
    session: AsyncSession,
    *,
    audience: str = "all",
) -> list[BotUser]:
    """Resolve recipients for a broadcast audience.

    ``users`` = customer-facing audience: role user **and** reseller.
    Dual-role shop owners (role=reseller) still receive «کاربران عادی» messages
    because they use the bot as customers too. Admins are excluded from ``users``.
    """
    q = select(BotUser).where(BotUser.is_blocked.is_(False), shop_member_clause(None))
    if audience == "users":
        q = q.where(BotUser.role.in_([Role.USER.value, Role.RESELLER.value]))
    elif audience == "resellers":
        q = q.where(BotUser.role == Role.RESELLER.value)
    elif audience == "admins":
        q = q.where(BotUser.role == Role.ADMIN.value)
    result = await session.execute(q.order_by(BotUser.id))
    return list(result.scalars().all())


async def list_broadcast_history(session: AsyncSession, *, limit: int = 50) -> list[BroadcastLog]:
    result = await session.execute(
        select(BroadcastLog).order_by(BroadcastLog.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def delete_broadcast_log(session: AsyncSession, log_id: int) -> bool:
    row = await session.get(BroadcastLog, int(log_id))
    # A queued or running broadcast is stopped first; deleting it would hide a send in progress.
    if not row or row.status in ("queued", "running"):
        return False
    await session.delete(row)
    await session.commit()
    return True


async def clear_broadcast_history(session: AsyncSession) -> int:
    from sqlalchemy import delete, func, select

    finished = BroadcastLog.status.not_in(("queued", "running"))
    count = await session.scalar(select(func.count()).select_from(BroadcastLog).where(finished)) or 0
    if count:
        await session.execute(delete(BroadcastLog).where(finished))
        await session.commit()
    return int(count)


async def send_broadcast(
    bot: Bot,
    session: AsyncSession,
    *,
    text: str,
    audience: str = "all",
    delay: float = 0.05,
    created_by: str | None = None,
) -> dict:
    """Send HTML text to matching users and persist history. Returns counts."""
    body = (text or "").strip()
    if not body:
        raise ValueError("متن پیام خالی است")
    if len(body) > 4000:
        raise ValueError("متن پیام خیلی طولانی است (حداکثر ۴۰۰۰)")

    # Keyset chunks — avoid loading the entire audience into memory.
    chunk_size = 250
    ok = 0
    fail = 0
    total = 0
    last_id = 0
    while True:
        # The platform bot token only reaches platform customers; shop customers
        # belong to their shop's bot.
        q = select(BotUser).where(
            BotUser.is_blocked.is_(False),
            shop_member_clause(None),
            BotUser.id > last_id,
        )
        if audience == "users":
            q = q.where(BotUser.role.in_([Role.USER.value, Role.RESELLER.value]))
        elif audience == "resellers":
            q = q.where(BotUser.role == Role.RESELLER.value)
        elif audience == "admins":
            q = q.where(BotUser.role == Role.ADMIN.value)
        chunk = list(
            (
                await session.execute(q.order_by(BotUser.id).limit(chunk_size))
            ).scalars().all()
        )
        if not chunk:
            break
        for u in chunk:
            total += 1
            last_id = int(u.id)
            try:
                await bot.send_message(u.telegram_id, body, parse_mode="HTML")
                ok += 1
            except Exception as e:
                fail += 1
                logger.debug("broadcast fail tg=%s: %s", u.telegram_id, e)
            if delay:
                await asyncio.sleep(delay)
        if len(chunk) < chunk_size:
            break

    log = BroadcastLog(
        audience=audience,
        text=body,
        total=total,
        ok_count=ok,
        fail_count=fail,
        created_by=(created_by or "")[:128] or None,
    )
    session.add(log)
    await session.commit()

    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "audience": audience,
        "log_id": log.id,
    }
