"""Persisted dismiss/snooze for computed panel inbox alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelInboxDismissal

logger = logging.getLogger(__name__)

MODE_24H = "24h"
MODE_FOREVER = "forever"
SNOOZE_HOURS = 24
MAX_SEEN_COUNT = 2_000_000_000

ALLOWED_ALERT_KEYS = frozenset(
    {
        "update",
        "ticket_alert",
        "shop_maintenance",
        "capacity_warn",
        "customer_alerts_off",
        "payg_suspended",
        "payg_low",
        "ac:pending",
        "ac:delivery",
        "ac:tickets",
        "ac:expiring",
        "ac:low_volume",
    }
)

# Alerts that are a single on/off notice in the inbox context; hiding one sets it to False.
_FLAG_ALERT_KEYS = ("shop_maintenance", "capacity_warn", "customer_alerts_off")


def staff_dismiss_key(staff: dict) -> str:
    """Stable per-staff key for persisted inbox dismissals."""
    try:
        pid = int(staff.get("org_principal_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        return f"op:{pid}"[:128]
    role = str(staff.get("role") or "")
    if staff.get("pg_staff_id"):
        uid = f"pgs{staff.get('pg_staff_id')}"
    elif staff.get("bot_user_id"):
        uid = f"b{staff.get('bot_user_id')}"
    elif staff.get("username"):
        uid = str(staff.get("username"))
    else:
        uid = str(staff.get("id") or "")
    return f"{role}:{uid}"[:128]


def staff_dismiss_key_candidates(staff: dict) -> list[str]:
    """Current + legacy keys (covers staff-id churn across releases)."""
    keys: list[str] = []

    def add(key: str) -> None:
        key = (key or "")[:128]
        if key and key not in keys:
            keys.append(key)

    add(staff_dismiss_key(staff))
    role = str(staff.get("role") or "")
    try:
        pid = int(staff.get("org_principal_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    username = str(staff.get("username") or "").strip()
    if pid > 0:
        add(f"op:{pid}")
        add(f"{role}:p{pid}")
        add(f"{role}:{pid}")
    if username:
        add(f"{role}:{username}")
    for uid in (
        staff.get("bot_user_id"),
        staff.get("pg_staff_id"),
        staff.get("id"),
    ):
        if uid is None or uid == "":
            continue
        add(f"{role}:{uid}")
        if staff.get("pg_staff_id") and uid == staff.get("pg_staff_id"):
            add(f"{role}:pgs{uid}")
        if staff.get("bot_user_id") and uid == staff.get("bot_user_id"):
            add(f"{role}:b{uid}")
    return keys


def _staff_dismissal_clause(staff: dict):
    """SQL filter matching canonical + every known legacy staff_key shape."""
    from sqlalchemy import or_

    keys = staff_dismiss_key_candidates(staff)
    clauses = []
    if keys:
        clauses.append(PanelInboxDismissal.staff_key.in_(keys))
    if not clauses:
        return None
    return or_(*clauses)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def alert_key_for_action_center(entry_key: str) -> str:
    return f"ac:{entry_key}"


def is_dismissed(
    row: PanelInboxDismissal,
    *,
    alert_active: bool,
    now: datetime | None = None,
) -> bool:
    """Return True when the alert should stay hidden."""
    now = now or _utcnow()
    dismissed_at = _aware(row.dismissed_at)
    if row.mode == MODE_24H:
        return now < dismissed_at + timedelta(hours=SNOOZE_HOURS)
    # forever: hide while the underlying condition is still active
    if row.mode == MODE_FOREVER:
        return bool(alert_active)
    return False


async def upsert_dismissal(
    session: AsyncSession,
    *,
    staff: dict,
    alert_key: str,
    mode: str,
    entity_id: str = "",
    seen_count: int = 0,
) -> None:
    key = staff_dismiss_key(staff)
    alert_key = (alert_key or "").strip()
    if alert_key not in ALLOWED_ALERT_KEYS:
        raise ValueError("اعلان نامعتبر")
    if mode not in (MODE_24H, MODE_FOREVER):
        raise ValueError("حالت حذف نامعتبر")
    entity_id = (entity_id or "").strip()[:64]
    seen_count = max(0, min(int(seen_count or 0), MAX_SEEN_COUNT))
    clause = _staff_dismissal_clause(staff)
    existing = None
    if clause is not None:
        rows = list(
            (
                await session.execute(
                    select(PanelInboxDismissal).where(
                        clause,
                        PanelInboxDismissal.alert_key == alert_key,
                        PanelInboxDismissal.entity_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if rows:
            existing = rows[0]
            # Collapse duplicates onto the canonical key
            for dup in rows[1:]:
                await session.delete(dup)

    if existing:
        existing.mode = mode
        existing.seen_count = seen_count
        existing.dismissed_at = _utcnow()
        existing.staff_key = key
    else:
        session.add(
            PanelInboxDismissal(
                staff_key=key,
                alert_key=alert_key,
                entity_id=entity_id,
                mode=mode,
                seen_count=seen_count,
                dismissed_at=_utcnow(),
            )
        )
    await session.commit()


async def load_dismissals(
    session: AsyncSession,
    staff: dict,
) -> list[PanelInboxDismissal]:
    clause = _staff_dismissal_clause(staff)
    if clause is None:
        return []
    return list(
        (await session.execute(select(PanelInboxDismissal).where(clause)))
        .scalars()
        .all()
    )


def _action_center_active_keys(ac: dict[str, Any]) -> dict[str, bool]:
    """Map action-center alert keys from counts (not only rendered entries)."""
    out: dict[str, bool] = {}
    if int(ac.get("pending") or 0) > 0:
        out["ac:pending"] = True
    if int(ac.get("failures") or 0) > 0:
        out["ac:delivery"] = True
    if int(ac.get("tickets") or 0) > 0:
        out["ac:tickets"] = True
    if int(ac.get("expiring") or 0) > 0:
        out["ac:expiring"] = True
    if int(ac.get("low_volume") or 0) > 0:
        out["ac:low_volume"] = True
    for entry in ac.get("entries") or []:
        ek = str(entry.get("key") or "")
        if ek:
            out[alert_key_for_action_center(ek)] = True
    return out


def _alert_active_map(ctx: dict[str, Any]) -> dict[str, bool]:
    """Which alert keys are currently active in a built inbox context."""
    pr = ctx.get("payg_risk") or {}
    active: dict[str, bool] = {
        "update": bool((ctx.get("update") or {}).get("update_available")),
        "shop_maintenance": bool(ctx.get("shop_maintenance")),
        "capacity_warn": bool(ctx.get("capacity_warn")),
        "customer_alerts_off": bool(ctx.get("customer_alerts_off")),
        "payg_suspended": bool(pr.get("suspended")),
        "payg_low": bool(pr.get("low")),
    }
    ta = ctx.get("ticket_alert")
    if ta:
        active["ticket_alert"] = True
    if ctx.get("action_center_ok", True):
        active.update(_action_center_active_keys(ctx.get("action_center") or {}))
    return active


_AC_COUNT_FIELDS = {
    "ac:pending": "pending",
    "ac:delivery": "failures",
    "ac:tickets": "tickets",
    "ac:expiring": "expiring",
    "ac:low_volume": "low_volume",
}


def alert_counts(ctx: dict[str, Any]) -> dict[str, int]:
    """How many underlying items each alert key currently represents."""
    pr = ctx.get("payg_risk") or {}
    counts: dict[str, int] = {
        "update": 1 if (ctx.get("update") or {}).get("update_available") else 0,
        "ticket_alert": 1 if ctx.get("ticket_alert") else 0,
        "shop_maintenance": 1 if ctx.get("shop_maintenance") else 0,
        "capacity_warn": 1 if ctx.get("capacity_warn") else 0,
        "customer_alerts_off": 1 if ctx.get("customer_alerts_off") else 0,
        "payg_suspended": len(pr.get("suspended") or []),
        "payg_low": len(pr.get("low") or []),
    }
    ac = ctx.get("action_center") or {}
    for alert_key, field in _AC_COUNT_FIELDS.items():
        counts[alert_key] = int(ac.get(field) or 0)
    return counts


def has_new_items(row: PanelInboxDismissal, current_count: int) -> bool:
    """True when more items exist now than when the user dismissed the alert."""
    seen = int(getattr(row, "seen_count", 0) or 0)
    return seen > 0 and int(current_count) > seen


def is_snooze_expired(row: PanelInboxDismissal, *, now: datetime | None = None) -> bool:
    if row.mode != MODE_24H:
        return False
    now = now or _utcnow()
    return now >= _aware(row.dismissed_at) + timedelta(hours=SNOOZE_HOURS)


async def cleanup_resolved_dismissals(
    session: AsyncSession,
    staff: dict,
    ctx: dict[str, Any],
) -> None:
    """Drop forever-dismiss rows once their alert is no longer active."""
    rows = await load_dismissals(session, staff)
    if not rows:
        return

    active = _alert_active_map(ctx)
    ac_ok = bool(ctx.get("action_center_ok", True))

    stale_ids: list[int] = []
    counts = alert_counts(ctx)
    for row in rows:
        if is_snooze_expired(row):
            stale_ids.append(int(row.id))
            continue
        if row.mode != MODE_FOREVER:
            continue
        if row.alert_key in counts and has_new_items(row, counts[row.alert_key]):
            stale_ids.append(int(row.id))
            continue
        # When action-center build failed, do not guess AC keys are gone —
        # still clean non-AC forever rows (update/capacity/payg/…).
        if not ac_ok and str(row.alert_key or "").startswith("ac:"):
            continue
        if row.alert_key == "ticket_alert":
            ta = ctx.get("ticket_alert")
            if not ta:
                stale_ids.append(int(row.id))
                continue
            if row.entity_id:
                eid = str(ta.get("ticket_id") or ta.get("id") or "")
                if eid != row.entity_id:
                    stale_ids.append(int(row.id))
            continue
        if not active.get(row.alert_key, False):
            stale_ids.append(int(row.id))
    if stale_ids:
        await session.execute(
            delete(PanelInboxDismissal).where(PanelInboxDismissal.id.in_(stale_ids))
        )
        await session.commit()


async def clear_staff_dismissals(session: AsyncSession, staff: dict) -> int:
    """Remove all persisted hide/snooze rows for this staff member."""
    clause = _staff_dismissal_clause(staff)
    if clause is None:
        return 0
    result = await session.execute(delete(PanelInboxDismissal).where(clause))
    await session.commit()
    removed = int(result.rowcount or 0)
    logger.info(
        "inbox dismiss reset staff=%s removed=%s keys=%s",
        staff_dismiss_key(staff),
        removed,
        staff_dismiss_key_candidates(staff)[:12],
    )
    return removed


def _row_matches(row: PanelInboxDismissal, alert_key: str, entity_id: str = "") -> bool:
    if row.alert_key != alert_key:
        return False
    row_eid = (row.entity_id or "").strip()
    check_eid = (entity_id or "").strip()
    if not row_eid:
        return True
    if not check_eid:
        return False
    return row_eid == check_eid


async def filter_action_center_for_staff(
    session: AsyncSession,
    staff: dict,
    action_center: dict[str, Any],
) -> dict[str, Any]:
    """Apply persisted dismissals to a dashboard work-queue payload."""
    dismissals = await load_dismissals(session, staff)
    if not dismissals:
        return action_center
    filtered = filter_inbox_context({"action_center": action_center}, dismissals)
    return filtered.get("action_center") or action_center


def filter_inbox_context(
    ctx: dict[str, Any],
    dismissals: list[PanelInboxDismissal],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply dismissals to a built inbox context (mutates copy)."""
    now = now or _utcnow()
    out = dict(ctx)
    counts = alert_counts(ctx)

    def hidden(alert_key: str, active: bool, entity_id: str = "") -> bool:
        for row in dismissals:
            if not _row_matches(row, alert_key, entity_id):
                continue
            if alert_key in counts and has_new_items(row, counts[alert_key]):
                continue
            if is_dismissed(row, alert_active=active, now=now):
                return True
        return False

    if out.get("update") and hidden("update", bool((out["update"] or {}).get("update_available"))):
        out["update"] = dict(out["update"])
        out["update"]["update_available"] = False

    if out.get("ticket_alert"):
        ta = out["ticket_alert"]
        eid = str(ta.get("ticket_id") or ta.get("id") or "")
        if hidden("ticket_alert", True, eid):
            out["ticket_alert"] = None

    for flag_key in _FLAG_ALERT_KEYS:
        if out.get(flag_key) and hidden(flag_key, True):
            out[flag_key] = False

    pr = dict(out.get("payg_risk") or {})
    if pr.get("suspended") and hidden("payg_suspended", True):
        pr["suspended"] = []
    if pr.get("low") and hidden("payg_low", True):
        pr["low"] = []
    pr["has_items"] = bool(pr.get("suspended") or pr.get("low"))
    out["payg_risk"] = pr

    ac = dict(out.get("action_center") or {})
    entries = []
    for entry in ac.get("entries") or []:
        ek = str(entry.get("key") or "")
        ak = alert_key_for_action_center(ek)
        if hidden(ak, True):
            continue
        entries.append(entry)
    ac["entries"] = entries
    ac["has_items"] = bool(entries)
    out["action_center"] = ac

    from app.services.panel_inbox import inbox_alert_count

    out["inbox_count"] = inbox_alert_count(out)
    out["inbox_has"] = out["inbox_count"] > 0
    return out
