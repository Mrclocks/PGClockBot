"""Users list ops: service summaries, filters, alert dots (no live PG fan-out).

Expire/volume prefer ``UserService.quota_*`` when ``quota_synced_at`` is set
(admin adjust / renew / live modal snapshot). Otherwise fall back to plan +
``created_at`` approximations (action-center parity) plus ``notified_*`` flags.
Live PasarGuard snapshots stay on the per-user edit modal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from sqlalchemy import and_, exists, not_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, Plan, Role, UserService
from app.services.formatting import format_bytes

UserFilter = Literal[
    "all",
    "expiring",
    "low_volume",
    "no_service",
    "blocked",
    "alerts",
]

VALID_FILTERS: frozenset[str] = frozenset(
    {"all", "expiring", "low_volume", "no_service", "blocked", "alerts"}
)

DEFAULT_EXPIRE_DAYS = 3
_GB = 1024**3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_users_filter(raw: str | None) -> UserFilter:
    key = (raw or "all").strip().lower()
    if key in VALID_FILTERS:
        return key  # type: ignore[return-value]
    return "all"


def parse_focus_uid(raw: str | None) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        uid = int(raw)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def users_list_href(
    *,
    filter_key: UserFilter | str = "all",
    uid: int | None = None,
    q: str | None = None,
) -> str:
    """Build a safe deep-link into ``/users`` (whitelist query keys only)."""
    parts: list[str] = []
    fk = normalize_users_filter(str(filter_key))
    if fk != "all":
        parts.append(f"filter={fk}")
    focus = parse_focus_uid(str(uid) if uid is not None else None)
    if focus:
        parts.append(f"uid={focus}")
    if q:
        from urllib.parse import quote

        parts.append(f"q={quote(str(q).strip()[:80], safe='')}")
    return "/users" if not parts else "/users?" + "&".join(parts)


def approx_expire_at(service: UserService, plan: Plan | None = None) -> datetime | None:
    """Approx expire = created_at + plan.duration_days (action-center parity)."""
    plan = plan if plan is not None else getattr(service, "plan", None)
    created = _aware(getattr(service, "created_at", None))
    if not created or plan is None:
        return None
    days = getattr(plan, "duration_days", None)
    if days is None:
        return None
    try:
        d = int(days)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return created + timedelta(days=d)


def days_until(dt: datetime | None, *, now: datetime | None = None) -> int | None:
    if dt is None:
        return None
    now = now or _utcnow()
    remaining = dt - now
    if remaining.total_seconds() <= 0:
        return 0
    return max(1, int((remaining.total_seconds() + 86399) // 86400))


def format_plan_volume(plan: Plan | None) -> str:
    if plan is None:
        return "—"
    raw = getattr(plan, "data_limit_gb", None)
    if raw is None:
        return "نامحدود"
    try:
        gb = float(raw)
    except (TypeError, ValueError):
        return "—"
    if gb <= 0:
        return "نامحدود"
    return format_bytes(gb * (1024**3))


@dataclass
class ServiceSummary:
    id: int
    label: str
    volume_text: str
    expire_text: str
    days_left: int | None
    expiring: bool
    low_volume: bool
    has_alert: bool
    sort_expire: float  # sooner first; inf if unknown
    sort_volume: float  # smaller plan GB first; inf if unlimited/unknown


@dataclass
class UserOpsRow:
    user: BotUser
    services: list[ServiceSummary] = field(default_factory=list)
    service_count: int = 0
    volume_text: str = "—"
    expire_text: str = "—"
    days_left: int | None = None
    expiring: bool = False
    low_volume: bool = False
    no_service: bool = True
    has_alert: bool = False
    urgency: int = 0  # higher = more urgent


def _service_label(svc: UserService, plan: Plan | None) -> str:
    name = (getattr(plan, "name", None) or "").strip() if plan else ""
    uname = (getattr(svc, "pg_username", None) or "").strip()
    if name and uname:
        return f"{name} · {uname}"
    return name or uname or f"#{int(svc.id)}"


def _summarize_service(
    svc: UserService,
    *,
    expire_days: int,
    now: datetime,
) -> ServiceSummary:
    plan = getattr(svc, "plan", None)
    synced = getattr(svc, "quota_synced_at", None) is not None
    if synced:
        exp = _aware(getattr(svc, "quota_expire_at", None))
    else:
        exp = approx_expire_at(svc, plan)
    left = days_until(exp, now=now)
    # Synced + no expire ⇒ unlimited time (never "expiring")
    expiring = bool(
        exp is not None and now <= exp <= now + timedelta(days=expire_days)
    )
    # Scheduler flag: still marked until renew resets it.
    low_volume = bool(getattr(svc, "notified_traffic", False))
    has_alert = expiring or low_volume or (
        bool(getattr(svc, "notified_expire", False)) and left is not None and left <= expire_days
    )
    if synced and exp is None:
        expire_text = "نامحدود"
        left = None
    elif exp is not None:
        if left is None:
            expire_text = "—"
        elif left <= 0:
            expire_text = "منقضی"
        else:
            expire_text = f"{left} روز"
    else:
        expire_text = "—"

    gb_sort = float("inf")
    if synced:
        try:
            lim_b = getattr(svc, "quota_data_limit_bytes", None)
            lim_n = 0 if lim_b is None else int(lim_b)
        except (TypeError, ValueError):
            lim_n = 0
        if lim_n <= 0:
            vol = "نامحدود"
        else:
            vol = format_bytes(lim_n)
            gb_sort = lim_n / _GB
    else:
        vol = format_plan_volume(plan)
        if plan is not None and getattr(plan, "data_limit_gb", None) is not None:
            try:
                gb_v = float(plan.data_limit_gb)
                if gb_v > 0:
                    gb_sort = gb_v
            except (TypeError, ValueError):
                pass

    if low_volume and vol != "—":
        volume_text = f"{vol} · کم"
    else:
        volume_text = vol

    return ServiceSummary(
        id=int(svc.id),
        label=_service_label(svc, plan),
        volume_text=volume_text,
        expire_text=expire_text,
        days_left=left,
        expiring=expiring,
        low_volume=low_volume,
        has_alert=has_alert,
        sort_expire=exp.timestamp() if exp is not None else float("inf"),
        sort_volume=gb_sort,
    )


def build_user_ops_row(
    user: BotUser,
    services: Iterable[UserService],
    *,
    expire_days: int = DEFAULT_EXPIRE_DAYS,
    now: datetime | None = None,
) -> UserOpsRow:
    expire_days = max(1, min(30, int(expire_days or DEFAULT_EXPIRE_DAYS)))
    now = now or _utcnow()
    snaps = [_summarize_service(s, expire_days=expire_days, now=now) for s in services]
    # Alerted services first (then soonest expiry) so UI default selection matches
    snaps.sort(key=lambda s: (0 if s.has_alert else 1, s.sort_expire, s.sort_volume, s.id))

    row = UserOpsRow(user=user, services=snaps, service_count=len(snaps))
    row.no_service = len(snaps) == 0
    if not snaps:
        return row

    # Critical summary: soonest expiry + tightest volume plan.
    critical_exp = min(snaps, key=lambda s: (s.sort_expire, s.id))
    critical_vol = min(snaps, key=lambda s: (s.sort_volume, s.id))
    row.expire_text = critical_exp.expire_text
    row.days_left = critical_exp.days_left
    row.volume_text = critical_vol.volume_text
    row.expiring = any(s.expiring for s in snaps)
    row.low_volume = any(s.low_volume for s in snaps)
    row.has_alert = any(s.has_alert for s in snaps)

    urgency = 0
    if user.is_blocked:
        urgency += 5
    if row.expiring:
        urgency += 40
        if row.days_left is not None:
            urgency += max(0, 10 - int(row.days_left))
    if row.low_volume:
        urgency += 25
    if row.no_service:
        urgency += 8
    if row.has_alert:
        urgency += 10
    row.urgency = urgency
    return row


async def load_services_by_user_ids(
    session: AsyncSession, user_ids: list[int]
) -> dict[int, list[UserService]]:
    if not user_ids:
        return {}
    rows = list(
        (
            await session.execute(
                select(UserService)
                .where(UserService.bot_user_id.in_(user_ids))
                .options(selectinload(UserService.plan))
                .order_by(UserService.id.desc())
            )
        ).scalars().all()
    )
    out: dict[int, list[UserService]] = {int(uid): [] for uid in user_ids}
    for svc in rows:
        bucket = out.get(int(svc.bot_user_id))
        if bucket is not None:
            bucket.append(svc)
    return out


def pure_reseller_clause():
    """Dual reseller+services stay visible; pure resellers live under /resellers."""
    has_shop_service = exists(
        select(UserService.id).where(UserService.bot_user_id == BotUser.id)
    )
    return and_(BotUser.role == Role.RESELLER.value, not_(has_shop_service))


def scoped_users_where(scope_reseller_id: int | None):
    """Platform shop → reseller_id IS NULL; tenant → matching reseller_id."""
    base = not_(pure_reseller_clause())
    if scope_reseller_id is None:
        return and_(base, BotUser.reseller_id.is_(None))
    return and_(base, BotUser.reseller_id == int(scope_reseller_id))


def filter_ops_rows(rows: list[UserOpsRow], filter_key: UserFilter) -> list[UserOpsRow]:
    if filter_key == "all":
        return rows
    if filter_key == "expiring":
        return [r for r in rows if r.expiring]
    if filter_key == "low_volume":
        return [r for r in rows if r.low_volume]
    if filter_key == "no_service":
        return [r for r in rows if r.no_service]
    if filter_key == "blocked":
        return [r for r in rows if bool(r.user.is_blocked)]
    if filter_key == "alerts":
        return [r for r in rows if r.has_alert]
    return rows


def sort_ops_rows(
    rows: list[UserOpsRow],
    *,
    focus_uid: int | None = None,
) -> list[UserOpsRow]:
    """Urgency desc; focused uid pinned first when present."""

    def key(r: UserOpsRow) -> tuple:
        focused = 0 if (focus_uid and int(r.user.id) == int(focus_uid)) else 1
        return (focused, -int(r.urgency), -int(r.user.id))

    return sorted(rows, key=key)


def summarize_ops_counts(rows: list[UserOpsRow]) -> dict[str, int]:
    return {
        "total": len(rows),
        "expiring": sum(1 for r in rows if r.expiring),
        "low_volume": sum(1 for r in rows if r.low_volume),
        "no_service": sum(1 for r in rows if r.no_service),
        "blocked": sum(1 for r in rows if r.user.is_blocked),
        "alerts": sum(1 for r in rows if r.has_alert),
    }


async def build_users_ops_page(
    session: AsyncSession,
    users: list[BotUser],
    *,
    filter_key: UserFilter | str = "all",
    focus_uid: int | None = None,
    expire_days: int = DEFAULT_EXPIRE_DAYS,
) -> tuple[list[UserOpsRow], dict[str, int], UserFilter]:
    fk = normalize_users_filter(str(filter_key))
    focus = parse_focus_uid(str(focus_uid) if focus_uid is not None else None)
    by_uid = await load_services_by_user_ids(session, [int(u.id) for u in users])
    now = _utcnow()
    all_rows = [
        build_user_ops_row(
            u, by_uid.get(int(u.id), []), expire_days=expire_days, now=now
        )
        for u in users
    ]
    counts = summarize_ops_counts(all_rows)
    filtered = filter_ops_rows(all_rows, fk)
    if focus:
        focused = next((r for r in all_rows if int(r.user.id) == int(focus)), None)
        if focused and not any(int(r.user.id) == int(focus) for r in filtered):
            filtered = [focused] + filtered
    ordered = sort_ops_rows(filtered, focus_uid=focus)
    return ordered, counts, fk


def bot_user_alert_flags(row: UserOpsRow) -> str:
    """Compact emoji prefix for Telegram admin/reseller lists."""
    bits: list[str] = []
    if row.user.is_blocked:
        bits.append("🚫")
    elif row.has_alert:
        bits.append("🔔")
    if row.expiring:
        bits.append("⏰")
    if row.low_volume:
        bits.append("📉")
    if not bits:
        if row.user.role == Role.RESELLER.value:
            bits.append("🤝")
        else:
            bits.append("👤")
    return "".join(bits)
