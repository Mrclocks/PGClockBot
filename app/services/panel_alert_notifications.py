"""Telegram pushes for panel alerts that otherwise only show up when somebody opens the panel.

An alert is a *state* (a new version exists, a shop is close to its capacity, failed deliveries are
waiting), not an event, so every state is kept in ``panel_alert_states`` and a message is sent only
when it turns on, grows or changes, never on every check. Recipients and the on/off switches are the
ordinary notification settings (``notify_panel_*``); this module only decides *whether* to send.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DeliveryFailure, PanelAlertState

logger = logging.getLogger(__name__)

ALERT_UPDATE = "update"
ALERT_CAPACITY = "capacity"
ALERT_FAILURES = "delivery_failures"

KEY_UPDATE = "notify_panel_update"
KEY_CAPACITY = "notify_panel_capacity"
KEY_FAILURES = "notify_panel_delivery_failures"

PLATFORM_SCOPE = "platform"
REPEAT_AFTER = timedelta(hours=6)  # a growing alert is repeated at most this often
CAPACITY_RECHECK = timedelta(minutes=30)
CAPACITY_HYSTERESIS_PCT = 5.0
DEFAULT_CAPACITY_WARN_PCT = 80.0


@dataclass(frozen=True, slots=True)
class Signal:
    """One alert state with the message to send when it is worth announcing."""

    shop_id: int | None
    alert: str
    notify_key: str
    count: int
    text: str
    path: str  # panel page the button opens (platform path; shops get their own settings page)
    fingerprint: str = ""


def scope_key(shop_id: int | None) -> str:
    return PLATFORM_SCOPE if shop_id is None else f"shop:{int(shop_id)}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _state(session: AsyncSession, scope: str, alert: str) -> PanelAlertState | None:
    return await session.scalar(
        select(PanelAlertState).where(PanelAlertState.scope_key == scope, PanelAlertState.alert == alert)
    )


async def claim_signal(session: AsyncSession, signal: Signal, *, now: datetime | None = None) -> bool:
    """Decide atomically whether ``signal`` must be sent now; the caller sends only on ``True``.

    A signal is sent when the alert turns on, its count grows or its fingerprint changes. While it
    stays on, a growing alert is repeated at most every ``REPEAT_AFTER``. When it turns off the state
    is cleared so the next occurrence is announced again.
    """
    now = now or _utcnow()
    if signal.count <= 0:
        await clear_signal(session, signal)
        return False
    scope = scope_key(signal.shop_id)
    row = await _state(session, scope, signal.alert)
    if row is None:
        session.add(PanelAlertState(
            scope_key=scope, alert=signal.alert, active=True, last_count=signal.count,
            fingerprint=signal.fingerprint, notified_at=now,
        ))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # another worker announced it first
            return False
        return True
    changed = (not row.active) or signal.count > row.last_count or signal.fingerprint != row.fingerprint
    if not changed:
        return False
    notified_at = _aware(row.notified_at)
    if row.active and signal.fingerprint == row.fingerprint and notified_at and now - notified_at < REPEAT_AFTER:
        return False
    result = await session.execute(
        update(PanelAlertState)
        .where(
            PanelAlertState.id == row.id,
            PanelAlertState.active == row.active,
            PanelAlertState.last_count == row.last_count,
            PanelAlertState.fingerprint == row.fingerprint,
        )
        .values(active=True, last_count=signal.count, fingerprint=signal.fingerprint, notified_at=now),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    return result.rowcount == 1


async def clear_signal(session: AsyncSession, signal: Signal) -> bool:
    """Turn an announced alert off; True when it was on (its last message should go).

    A plain read first, so the common «still off» case writes nothing; the update itself is
    conditional, so two runs cannot both clear the same alert.
    """
    row = await _state(session, scope_key(signal.shop_id), signal.alert)
    if row is None or not row.active:
        return False
    result = await session.execute(
        update(PanelAlertState)
        .where(PanelAlertState.id == row.id, PanelAlertState.active.is_(True))
        .values(active=False, last_count=0, fingerprint=""),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    return result.rowcount == 1


async def release_signal(session: AsyncSession, signal: Signal) -> None:
    """Nobody received the message: forget it was sent so the next check tries again."""
    await session.execute(
        update(PanelAlertState)
        .where(PanelAlertState.scope_key == scope_key(signal.shop_id), PanelAlertState.alert == signal.alert)
        .values(active=False, notified_at=None),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def capacity_due(session: AsyncSession, shop_ids: list[int], *, now: datetime | None = None, limit: int = 50) -> list[int]:
    """Shops whose capacity has not been checked for ``CAPACITY_RECHECK`` (never-checked first)."""
    now = now or _utcnow()
    rows = (
        await session.execute(
            select(PanelAlertState.scope_key, PanelAlertState.checked_at).where(PanelAlertState.alert == ALERT_CAPACITY)
        )
    ).all()
    checked = {key: _aware(at) for key, at in rows}
    due = [sid for sid in shop_ids if (at := checked.get(scope_key(sid))) is None or now - at >= CAPACITY_RECHECK]
    due.sort(key=lambda sid: checked.get(scope_key(sid)) or datetime.min.replace(tzinfo=timezone.utc))
    return due[:limit]


async def mark_capacity_checked(session: AsyncSession, shop_id: int, *, now: datetime | None = None) -> None:
    now = now or _utcnow()
    scope = scope_key(shop_id)
    row = await _state(session, scope, ALERT_CAPACITY)
    if row is None:
        session.add(PanelAlertState(scope_key=scope, alert=ALERT_CAPACITY, checked_at=now))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
        return
    await session.execute(
        update(PanelAlertState).where(PanelAlertState.id == row.id).values(checked_at=now),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def failure_signals(session: AsyncSession) -> list[Signal]:
    """Open failed deliveries per owner (platform or shop), one count per order."""
    rows = (
        await session.execute(
            select(DeliveryFailure.reseller_id, func.count(func.distinct(DeliveryFailure.order_id)))
            .where(DeliveryFailure.resolved_at.is_(None))
            .group_by(DeliveryFailure.reseller_id)
        )
    ).all()
    counts = {(int(rid) if rid else None): int(n) for rid, n in rows}
    # Owners that had a signal before but have no failures now must be cleared, so they are listed too.
    known = (
        await session.execute(
            select(PanelAlertState.scope_key).where(PanelAlertState.alert == ALERT_FAILURES, PanelAlertState.active.is_(True))
        )
    ).scalars().all()
    for key in known:
        owner = None if key == PLATFORM_SCOPE else int(key.split(":", 1)[1])
        counts.setdefault(owner, 0)
    return [
        Signal(
            shop_id=owner, alert=ALERT_FAILURES, notify_key=KEY_FAILURES, count=count,
            text=failures_text(count), path="/finance?tab=delivery",
        )
        for owner, count in counts.items()
    ]


def failures_text(count: int) -> str:
    return f"📦 <b>{int(count)}</b> تحویل ناموفق در انتظار بررسی است.\nاز مدیریت مالی ← تحویل ناموفق ادامه تحویل بدهید یا از لیست حذف کنید."


def update_signal(info: dict | None) -> Signal | None:
    if not info:
        return None
    available = bool(info.get("update_available"))
    remote = str(info.get("remote_version") or "")
    local = str(info.get("version") or "")
    text = (
        f"🔔 نسخه جدید پنل آماده است: <code>{html.escape(remote)}</code>\n"
        f"نسخه فعلی: <code>{html.escape(local)}</code>\nاز تنظیمات ← آپدیت می‌توانید به‌روزرسانی کنید."
    )
    return Signal(
        shop_id=None, alert=ALERT_UPDATE, notify_key=KEY_UPDATE, count=1 if available else 0,
        text=text, path="/settings?tab=update", fingerprint=remote[:64],
    )


def capacity_percent(admin: dict | None) -> float | None:
    """Highest used share of the admin's user and traffic limits, ``None`` when nothing is limited."""
    from app.services.pg_quota import _as_int, extract_admin_capacity

    if not isinstance(admin, dict):
        return None
    role = admin.get("role") if isinstance(admin.get("role"), dict) else None
    data_limit, max_users = extract_admin_capacity(admin, role)
    used_users = _as_int(admin.get("total_users")) or _as_int(admin.get("users_count")) or 0
    used_traffic = _as_int(admin.get("used_traffic")) or _as_int(admin.get("traffic_used")) or 0
    shares = [100.0 * used / limit for used, limit in ((used_users, max_users), (used_traffic, data_limit)) if limit > 0]
    return max(shares) if shares else None


def capacity_signal(shop_id: int, percent: float | None, threshold: float) -> Signal | None:
    """``None`` means «leave the state as it is» (inside the hysteresis band)."""
    if percent is None or percent < threshold - CAPACITY_HYSTERESIS_PCT:
        count = 0
    elif percent >= threshold:
        count = 1
    else:
        return None
    text = (
        f"⚠️ ظرفیت پنل پاسارگارد فروشگاه نزدیک پر شدن است ({percent or 0:.0f}٪ از سقف).\n"
        "از وب‌پنل ← خانه وضعیت سهمیه را بررسی کنید."
    )
    return Signal(shop_id=shop_id, alert=ALERT_CAPACITY, notify_key=KEY_CAPACITY, count=count, text=text, path="/home")
