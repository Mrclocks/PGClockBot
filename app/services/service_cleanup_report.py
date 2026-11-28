"""Why the expired-service cleanup has not removed a service yet, for the cleanup page.

Two kinds of reasons:
* a failure the worker met on its last try (the panel could not be read, the delete did not go
  through), stored on the row by ``service_cleanup_errors``;
* a state that holds the service back on purpose (an open order, automatic renewal, a blocked
  customer, no Telegram chat for the warning, the warning not sent long enough ago), read live.

Everything here only reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import ColumnElement, and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, ServiceAutomation, UserService
from app.services.service_cleanup import (
    KIND_TIME,
    KIND_VOLUME,
    STAGE_NOTICE,
    CleanupPolicy,
    PreviewRow,
    eligible_clauses,
    local_date,
    not_held_clause,
    shop_services_clause,
    time_due_clause,
    volume_due_clause,
)
from app.services.service_cleanup_delete import MIN_WARNING_GAP, OPEN_ORDER_STATUSES
from app.services.service_cleanup_errors import ERROR_DELETE_FAILED, ERROR_PANEL_UNREADABLE
from app.services.service_cleanup_notices import stage_column

OPEN_ORDER = "open_order"
AUTO_RENEW = "auto_renew"
CUSTOMER_BLOCKED = "customer_blocked"
NO_CHAT = "no_chat"
AWAITING_NOTICE = "awaiting_notice"
NEXT_RUN = "next_run"
# Display order and text; a failure comes first because staff can usually fix it.
REASON_LABELS = {
    ERROR_DELETE_FAILED: "حذف در پنل انجام نشد؛ دوباره تلاش می‌شود",
    ERROR_PANEL_UNREADABLE: "پنل برای این سرویس جواب نداد",
    OPEN_ORDER: "سفارش باز یا پرداخت‌نشده دارد",
    AUTO_RENEW: "تمدید خودکار روشن است",
    CUSTOMER_BLOCKED: "مشتری مسدود است",
    NO_CHAT: "مشتری چت تلگرام ندارد و هشدار نمی‌گیرد",
    AWAITING_NOTICE: "در انتظار هشدار به مشتری",
    NEXT_RUN: "در نوبت حذف در اجرای بعدی",
}
FAILURE_CODES = (ERROR_DELETE_FAILED, ERROR_PANEL_UNREADABLE)
# Badge tone per reason: a failure needs attention, a deliberate hold is neutral.
REASON_TONES = {ERROR_DELETE_FAILED: "danger", ERROR_PANEL_UNREADABLE: "danger", NEXT_RUN: "info"}


@dataclass(frozen=True, slots=True)
class ReasonCount:
    code: str
    label: str
    count: int
    last_at: str = ""  # date of the latest failure, only for failures
    tone: str = "neutral"


def _reason(code: str, count: int, last_at: datetime | None = None) -> ReasonCount:
    return ReasonCount(code, REASON_LABELS[code], count, local_date(last_at), REASON_TONES.get(code, "neutral"))


def _hold_reason(kind: str, now: datetime) -> ColumnElement[str]:
    """The first deliberate reason that holds a due service back, or ``next_run`` when nothing does."""
    open_order = select(Order.id).where(Order.service_id == UserService.id, Order.status.in_(OPEN_ORDER_STATUSES))
    auto_renew = select(ServiceAutomation.service_id).where(
        ServiceAutomation.service_id == UserService.id, ServiceAutomation.renew_enabled.is_(True)
    )
    not_warned = and_(
        UserService.panel_missing_at.is_(None),
        or_(
            stage_column(kind) < STAGE_NOTICE,
            UserService.cleanup_warned_at.is_(None),
            UserService.cleanup_warned_at > now - MIN_WARNING_GAP,
        ),
    )
    return case(
        (open_order.exists(), literal(OPEN_ORDER)),
        (auto_renew.exists(), literal(AUTO_RENEW)),
        (BotUser.is_blocked.is_(True), literal(CUSTOMER_BLOCKED)),
        (and_(UserService.panel_missing_at.is_(None), func.coalesce(BotUser.telegram_id, 0) == 0), literal(NO_CHAT)),
        (not_warned, literal(AWAITING_NOTICE)),
        else_=literal(NEXT_RUN),
    )


def _row_reason(kind: str, now: datetime) -> ColumnElement[str]:
    return func.coalesce(UserService.cleanup_error, _hold_reason(kind, now))


def _due_clauses(policy: CleanupPolicy, now: datetime) -> list[tuple[str, ColumnElement[bool]]]:
    due = ((KIND_TIME, time_due_clause(policy, now)), (KIND_VOLUME, volume_due_clause(policy, now)))
    return [(kind, clause) for kind, clause in due if clause is not None]


async def _failure_counts(session: AsyncSession, shop_id: int | None) -> list[ReasonCount]:
    rows = (
        await session.execute(
            select(UserService.cleanup_error, func.count(UserService.id), func.max(UserService.cleanup_error_at))
            .where(shop_services_clause(shop_id), *eligible_clauses(), UserService.cleanup_error.is_not(None))
            .group_by(UserService.cleanup_error)
        )
    ).all()
    found = {str(code): (int(count), last) for code, count, last in rows}
    return [_reason(code, *found[code]) for code in FAILURE_CODES if code in found]


async def reason_summary(
    session: AsyncSession, *, shop_id: int | None, policy: CleanupPolicy, now: datetime | None = None
) -> list[ReasonCount]:
    """Failures of the last tries in this scope, then why the services past their deadline are still here.

    Each due service is counted once, under the first reason that applies; services with a failure
    are counted under the failure only.
    """
    now = now or datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    for kind, due in _due_clauses(policy, now):
        reason = _hold_reason(kind, now).label("reason")
        rows = (
            await session.execute(
                select(reason, func.count(UserService.id))
                .select_from(UserService)
                .join(BotUser, BotUser.id == UserService.bot_user_id)
                .where(shop_services_clause(shop_id), *eligible_clauses(), not_held_clause(now), due,
                       UserService.cleanup_error.is_(None))
                .group_by(reason)
            )
        ).all()
        for code, count in rows:
            counts[str(code)] = counts.get(str(code), 0) + int(count)
    held = [_reason(code, counts[code]) for code in REASON_LABELS if code in counts]
    return await _failure_counts(session, shop_id) + held


async def due_row_reasons(
    session: AsyncSession, rows: list[PreviewRow], *, now: datetime | None = None
) -> dict[int, ReasonCount]:
    """Why each preview row that is already past its deadline is still here (one query per rule)."""
    now = now or datetime.now(timezone.utc)
    reasons: dict[int, ReasonCount] = {}
    for kind in (KIND_TIME, KIND_VOLUME):
        ids = [row.service_id for row in rows if row.days_left == 0 and row.kind_code == kind]
        if not ids:
            continue
        found = await session.execute(
            select(UserService.id, _row_reason(kind, now))
            .join(BotUser, BotUser.id == UserService.bot_user_id)
            .where(UserService.id.in_(ids))
        )
        reasons.update({int(service_id): _reason(str(code), 0) for service_id, code in found.all()})
    return reasons
