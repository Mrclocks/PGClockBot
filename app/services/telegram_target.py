"""Who staff mean when they type a person: a numeric Telegram id, ``@username`` or a ``t.me`` link.

Telegram does not turn a username into an id for a bot, so a username can only be matched against
the people who already started this bot. Usernames move between accounts: when more than one stored
person carries the same username (an old row and the account that has it now), the lookup refuses
rather than guess, and staff use the numeric id or the contact picker instead. The id is always what
gets stored; the username is only a way to find it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.user_search import clean_handle

# Telegram usernames: 5-32 characters, letters, digits and underscores, starting with a letter.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
# Telegram user ids are positive and fit in 52 bits.
TELEGRAM_ID_MAX = 2**52

TARGET_INVALID = "آیدی عددی تلگرام یا یوزرنیم را درست بفرستید؛ مثلاً 123456789 یا @username"
TARGET_AMBIGUOUS = "چند نفر با این یوزرنیم در ربات ثبت شده‌اند؛ با آیدی عددی یا «انتخاب از مخاطبین» اضافه کنید."
ID_NOT_FOUND = "کاربری با این آیدی در ربات نیست؛ باید یک بار ربات را استارت کرده باشد."
USERNAME_NOT_FOUND = (
    "کسی با یوزرنیم @{username} ربات را استارت نکرده است. تلگرام یوزرنیم را به آیدی تبدیل نمی‌کند؛ "
    "باید یک بار ربات را استارت کند، یا در ربات از «انتخاب از مخاطبین» استفاده کنید."
)


class TargetNotFound(ValueError):
    """Nobody matched; ``username`` is set when the person was named by username."""

    def __init__(self, message: str, *, username: str | None = None) -> None:
        super().__init__(message)
        self.username = username


@dataclass(frozen=True, slots=True)
class TelegramTarget:
    """What staff typed, parsed: exactly one of ``telegram_id`` and ``username`` is set."""

    telegram_id: int | None = None
    username: str | None = None


def parse_telegram_target(raw: str | None) -> TelegramTarget:
    """``TelegramTarget`` for an id, ``@name``, ``name`` or ``t.me/name``; ``ValueError`` otherwise."""
    text = clean_handle(raw)
    if text.isdigit():
        value = int(text)
        if 0 < value < TELEGRAM_ID_MAX:
            return TelegramTarget(telegram_id=value)
        raise ValueError(TARGET_INVALID)
    if USERNAME_RE.match(text):
        return TelegramTarget(username=text)
    raise ValueError(TARGET_INVALID)


async def find_target_user(session: AsyncSession, raw: str | None, *, scope: ColumnElement[bool]) -> BotUser:
    """The one person in ``scope`` that ``raw`` names; ``TargetNotFound`` or ``ValueError`` (safe to show)."""
    target = parse_telegram_target(raw)
    if target.telegram_id is not None:
        user = await session.scalar(
            select(BotUser).where(scope, BotUser.telegram_id == target.telegram_id).limit(1)
        )
        if user is None:
            raise TargetNotFound(ID_NOT_FOUND)
        return user
    rows = list(
        await session.scalars(
            select(BotUser)
            .where(scope, func.lower(BotUser.username) == str(target.username).lower())
            .order_by(BotUser.id)
            .limit(2)
        )
    )
    if not rows:
        raise TargetNotFound(USERNAME_NOT_FOUND.format(username=target.username), username=target.username)
    if len(rows) > 1:
        raise ValueError(TARGET_AMBIGUOUS)
    return rows[0]
