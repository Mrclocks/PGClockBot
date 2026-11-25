"""Why a group message did not reach someone, and who a tenant's bot can no longer message.

The broadcast worker records one row per recipient it did not reach, with the kind of reason read from Telegram's
answer (Telegram's own text is never stored or shown). People who blocked the bot, deleted their account or never
started it are also marked unreachable for that tenant's bot (``shop_key`` 0 is the platform bot), so later group
messages skip them; their next /start in that bot clears the mark. The history detail page shows the reasons.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import ColumnElement, delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, BroadcastFailure, BroadcastLog, BroadcastUnreachable

KIND_BLOCKED = "blocked"
KIND_DEACTIVATED = "deactivated"
KIND_NOT_STARTED = "not_started"
KIND_FORBIDDEN = "forbidden"
KIND_BAD_MESSAGE = "bad_message"
KIND_UNKNOWN = "unknown"
# Telegram's answer says the person cannot get any message from this bot until they start it again.
UNREACHABLE_KINDS = frozenset({KIND_BLOCKED, KIND_DEACTIVATED, KIND_NOT_STARTED})
# Lower-case fragments of Telegram's error descriptions, checked in order.
_DESCRIPTION_KINDS: tuple[tuple[str, str], ...] = (
    ("blocked by the user", KIND_BLOCKED),
    ("user is deactivated", KIND_DEACTIVATED),
    ("chat not found", KIND_NOT_STARTED),
    ("can't initiate conversation", KIND_NOT_STARTED),
    ("peer_id_invalid", KIND_NOT_STARTED),
    ("user not found", KIND_NOT_STARTED),
)


@dataclass(frozen=True, slots=True)
class FailureKind:
    key: str
    label: str
    hint: str


FAILURE_KINDS: dict[str, FailureKind] = {
    kind.key: kind
    for kind in (
        FailureKind(KIND_BLOCKED, "ربات را بلاک کرده‌اند", "تا دوباره ربات را استارت نکنند، پیام گروهی برایشان فرستاده نمی‌شود."),
        FailureKind(KIND_DEACTIVATED, "حساب تلگرامشان حذف شده", "به این حساب‌ها دیگر پیامی نمی‌رسد."),
        FailureKind(
            KIND_NOT_STARTED,
            "ربات را استارت نکرده‌اند",
            "تلگرام اجازه نمی‌دهد ربات اول پیام بدهد؛ بعد از استارت دوباره پیام می‌گیرند.",
        ),
        FailureKind(KIND_FORBIDDEN, "تلگرام اجازهٔ ارسال نداد", "معمولاً محدودیتی روی حساب گیرنده است."),
        FailureKind(
            KIND_BAD_MESSAGE,
            "تلگرام پیام را نپذیرفت",
            "معمولاً لینک یا دکمهٔ نامعتبر یا قالب‌بندی متن است؛ متن و دکمه‌ها را بررسی کنید.",
        ),
        FailureKind(
            KIND_UNKNOWN,
            "نامشخص",
            "اتصال به تلگرام وسط ارسال قطع شد و معلوم نیست پیام رسیده یا نه؛ برای جلوگیری از پیام تکراری دوباره فرستاده نمی‌شود.",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class ReasonCount:
    kind: FailureKind
    count: int


@dataclass(frozen=True, slots=True)
class BroadcastReport:
    """One broadcast of a tenant and why its failed and unknown recipients were not reached."""

    log: BroadcastLog
    reasons: list[ReasonCount]
    # Failures counted before reasons were recorded (older broadcasts).
    unrecorded: int


def shop_key(shop_id: int | None) -> int:
    return 0 if shop_id is None else int(shop_id)


def failure_kind(exc: TelegramAPIError) -> str:
    """The reason kind of a Telegram refusal (``TelegramForbiddenError`` or ``TelegramBadRequest``)."""
    description = str(getattr(exc, "message", "") or exc).lower()
    for fragment, kind in _DESCRIPTION_KINDS:
        if fragment in description:
            return kind
    return KIND_FORBIDDEN if isinstance(exc, TelegramForbiddenError) else KIND_BAD_MESSAGE


async def record_failure(session: AsyncSession, *, broadcast_id: int, user_id: int, kind: str) -> None:
    """Add the recipient's reason in the caller's transaction; a repeat for the same recipient is ignored."""
    await session.execute(
        insert(BroadcastFailure)
        .values(broadcast_id=int(broadcast_id), bot_user_id=int(user_id), kind=kind)
        .on_conflict_do_nothing(index_elements=["broadcast_id", "bot_user_id"])
    )


async def mark_unreachable(session: AsyncSession, *, user_id: int, shop_id: int | None, kind: str) -> None:
    """In the caller's transaction, skip this person in the tenant's later group messages when ``kind`` says so."""
    if kind not in UNREACHABLE_KINDS:
        return
    statement = insert(BroadcastUnreachable).values(user_id=int(user_id), shop_key=shop_key(shop_id), reason=kind)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "shop_key"], set_={"reason": kind, "marked_at": func.now()}
        )
    )


async def mark_reachable(session: AsyncSession, *, user_id: int, shop_id: int | None) -> None:
    """The person started the tenant's bot again: include them in its group messages."""
    cleared = await session.execute(
        delete(BroadcastUnreachable).where(
            BroadcastUnreachable.user_id == int(user_id), BroadcastUnreachable.shop_key == shop_key(shop_id)
        )
    )
    if cleared.rowcount:
        await session.commit()


def unreachable_clause(shop_id: int | None) -> ColumnElement[bool]:
    """``BotUser`` rows the tenant's bot cannot message."""
    return exists(
        select(BroadcastUnreachable.user_id).where(
            BroadcastUnreachable.user_id == BotUser.id, BroadcastUnreachable.shop_key == shop_key(shop_id)
        )
    )


def _tenant(shop_id: int | None) -> ColumnElement[bool]:
    return BroadcastLog.shop_id.is_(None) if shop_id is None else BroadcastLog.shop_id == int(shop_id)


async def broadcast_report(session: AsyncSession, log_id: int, *, shop_id: int | None) -> BroadcastReport | None:
    """The tenant's broadcast with its reasons, most common first; None when it is not this tenant's."""
    log = await session.scalar(select(BroadcastLog).where(BroadcastLog.id == int(log_id), _tenant(shop_id)))
    if log is None:
        return None
    rows = (
        await session.execute(
            select(BroadcastFailure.kind, func.count())
            .where(BroadcastFailure.broadcast_id == log.id)
            .group_by(BroadcastFailure.kind)
            .order_by(func.count().desc(), BroadcastFailure.kind)
        )
    ).all()
    reasons = [ReasonCount(FAILURE_KINDS.get(kind, FAILURE_KINDS[KIND_BAD_MESSAGE]), int(count)) for kind, count in rows]
    missed = int(log.fail_count or 0) + int(log.review_count or 0)
    return BroadcastReport(log=log, reasons=reasons, unrecorded=max(0, missed - sum(r.count for r in reasons)))
