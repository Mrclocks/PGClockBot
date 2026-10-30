"""Finding bot users by what staff actually type: a numeric id or a Telegram @username.

Staff paste usernames as ``@name``, ``name`` or a ``t.me/name`` link; Telegram usernames
are case-insensitive. Every query here is combined by the caller with its own shop scope.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.list_query import ilike_pattern, normalize_search_q

SEARCH_LIMIT = 500
RESULT_LIMIT = 10
MIN_QUERY_LENGTH = 2
_LINK_PREFIX = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/", re.IGNORECASE)
_ID_MAX = 2**63 - 1


def clean_handle(raw: str | None) -> str:
    """``@Name``, ``t.me/Name`` or ``Name`` → ``Name``; other text is only trimmed."""
    text = _LINK_PREFIX.sub("", normalize_search_q(raw))
    return text[1:].strip() if text.startswith("@") else text


def _numeric(text: str) -> int | None:
    if not text.isdigit():
        return None
    value = int(text)
    return value if value <= _ID_MAX else None


def bot_user_search_clause(raw: str | None) -> ColumnElement[bool] | None:
    """Rows whose id, Telegram id, username or name match; None for an empty query."""
    text = clean_handle(raw)
    if not text:
        return None
    pattern = ilike_pattern(text.casefold())
    clauses = [
        func.lower(BotUser.username).like(pattern, escape="\\"),
        func.lower(BotUser.full_name).like(pattern, escape="\\"),
    ]
    number = _numeric(text)
    if number is not None:
        clauses += [BotUser.id == number, BotUser.telegram_id == number]
    return or_(*clauses)


async def find_bot_user(
    session: AsyncSession, raw: str | None, *, scope: ColumnElement[bool]
) -> BotUser | None:
    """The one user in ``scope`` with this numeric Telegram id or exact @username."""
    text = clean_handle(raw)
    if not text:
        return None
    number = _numeric(text)
    match = (
        BotUser.telegram_id == number
        if number is not None
        else func.lower(BotUser.username) == text.casefold()
    )
    return await session.scalar(select(BotUser).where(scope, match).order_by(BotUser.id).limit(1))


async def with_search_matches(
    session: AsyncSession, recent: list[BotUser], scope: ColumnElement[bool], raw: str | None
) -> list[BotUser]:
    """``recent`` plus every user in ``scope`` matching ``raw``, so a search is not limited
    to the latest rows; newest first, each user once."""
    clause = bot_user_search_clause(raw)
    if clause is None:
        return recent
    matches = await session.scalars(
        select(BotUser).where(scope, clause).order_by(BotUser.id.desc()).limit(SEARCH_LIMIT)
    )
    by_id = {int(u.id): u for u in recent}
    for user in matches:
        by_id.setdefault(int(user.id), user)
    return sorted(by_id.values(), key=lambda u: int(u.id), reverse=True)


async def search_bot_users(
    session: AsyncSession, raw: str | None, *, scope: ColumnElement[bool], limit: int = RESULT_LIMIT
) -> tuple[list[BotUser], bool]:
    """Newest users in ``scope`` whose name, @username or id match; True when more exist.

    A text query needs at least two characters, so a single letter cannot list the whole shop.
    """
    text = clean_handle(raw)
    clause = bot_user_search_clause(raw)
    if clause is None or (_numeric(text) is None and len(text) < MIN_QUERY_LENGTH):
        return [], False
    rows = list(await session.scalars(select(BotUser).where(scope, clause).order_by(BotUser.id.desc()).limit(limit + 1)))
    return rows[:limit], len(rows) > limit
