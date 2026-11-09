"""Policy for deleting customer services that expired (or ran out of volume) and were not renewed.

Each shop (``None`` = the platform) sets its own rule for its own customers' services:
* time: a service that expired and was not renewed within N days is deleted;
* volume: a service whose volume is finished is deleted after its own, separate number of days.
Both are off by default. The days count from the later of the expiry and the moment the rule was
switched on, so switching it on never deletes an old backlog at once: those services are warned first.

This module holds the policy, who may change it, and the read-only preview. The worker that
warns and deletes lives elsewhere and re-reads the panel before every deletion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, UserService
from app.services.authz import authz_from_staff, can_shop
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import get_all_settings, on, set_settings_bulk

CLEANUP_PERMISSION = "service_cleanup"
DISPLAY_ZONE = ZoneInfo("Asia/Tehran")

KEY_TIME_ON = "expired_cleanup_enabled"
KEY_TIME_DAYS = "expired_cleanup_days"
KEY_TIME_ENABLED_AT = "expired_cleanup_enabled_at"
KEY_WARN_FIRST = "expired_cleanup_warn_first"
KEY_WARN_LAST = "expired_cleanup_warn_last"
KEY_VOLUME_ON = "volume_cleanup_enabled"
KEY_VOLUME_DAYS = "volume_cleanup_days"
KEY_VOLUME_ENABLED_AT = "volume_cleanup_enabled_at"
SETTING_DEFAULTS = {
    KEY_TIME_ON: "0",
    KEY_TIME_DAYS: "7",
    KEY_TIME_ENABLED_AT: "",
    KEY_WARN_FIRST: "3",
    KEY_WARN_LAST: "1",
    KEY_VOLUME_ON: "0",
    KEY_VOLUME_DAYS: "30",
    KEY_VOLUME_ENABLED_AT: "",
}
MIN_DAYS = 1
MAX_DAYS = 365
DEFAULT_TIME_DAYS = 7
DEFAULT_VOLUME_DAYS = 30
PREVIEW_SOON_DAYS = 7
PREVIEW_SAMPLE = 20
USERNAME_PREVIEW_MAX = 64


class CleanupPolicyError(ValueError):
    """The submitted rule is invalid; the message is safe to show staff."""


@dataclass(frozen=True, slots=True)
class CleanupPolicy:
    time_on: bool = False
    time_days: int = DEFAULT_TIME_DAYS
    warn_first: int = 3
    warn_last: int = 1
    volume_on: bool = False
    volume_days: int = DEFAULT_VOLUME_DAYS
    time_enabled_at: datetime | None = None
    volume_enabled_at: datetime | None = None

    def warning_days(self) -> list[int]:
        """Days before the deletion at which a warning goes out, earliest first (0 = switched off)."""
        return [d for d in (self.warn_first, self.warn_last) if d > 0]


def cleanup_scope(staff: dict) -> int | None:
    """Shop whose services ``staff`` may configure (``None`` = platform); raises ``PermissionError``."""
    if is_platform_admin(staff):
        return None
    if not can_shop(authz_from_staff(staff), CLEANUP_PERMISSION):
        raise PermissionError("دسترسی پاکسازی سرویس‌ها ندارید")
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def _int(raw: object, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _moment(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def policy_from_settings(settings: Mapping[str, str]) -> CleanupPolicy:
    """The saved rule; values that are missing or out of range fall back to safe defaults."""
    time_days = _int(settings.get(KEY_TIME_DAYS), DEFAULT_TIME_DAYS)
    volume_days = _int(settings.get(KEY_VOLUME_DAYS), DEFAULT_VOLUME_DAYS)
    time_days = time_days if MIN_DAYS <= time_days <= MAX_DAYS else DEFAULT_TIME_DAYS
    volume_days = volume_days if MIN_DAYS <= volume_days <= MAX_DAYS else DEFAULT_VOLUME_DAYS
    return CleanupPolicy(
        time_on=on(settings.get(KEY_TIME_ON, "0")),
        time_days=time_days,
        warn_first=max(0, _int(settings.get(KEY_WARN_FIRST), 3)),
        warn_last=max(0, _int(settings.get(KEY_WARN_LAST), 1)),
        volume_on=on(settings.get(KEY_VOLUME_ON, "0")),
        volume_days=volume_days,
        time_enabled_at=_moment(settings.get(KEY_TIME_ENABLED_AT)),
        volume_enabled_at=_moment(settings.get(KEY_VOLUME_ENABLED_AT)),
    )


def _whole_days(raw: object, label: str) -> int:
    text = str(raw or "").strip()
    if not text.isdigit() or len(text) > 4:
        raise CleanupPolicyError(f"{label} باید عدد صحیح باشد")
    return int(text)


def parse_policy_form(form: Mapping[str, object]) -> CleanupPolicy:
    """Validate a submitted form into a policy; raises ``CleanupPolicyError``."""
    time_days = _whole_days(form.get("time_days"), "مهلت سرویس منقضی")
    volume_days = _whole_days(form.get("volume_days"), "مهلت سرویس بدون حجم")
    warn_first = _whole_days(form.get("warn_first") or "0", "هشدار اول")
    warn_last = _whole_days(form.get("warn_last") or "0", "هشدار آخر")
    for label, days in (("مهلت سرویس منقضی", time_days), ("مهلت سرویس بدون حجم", volume_days)):
        if not MIN_DAYS <= days <= MAX_DAYS:
            raise CleanupPolicyError(f"{label} باید بین {MIN_DAYS} و {MAX_DAYS} روز باشد")
    for label, days in (("هشدار اول", warn_first), ("هشدار آخر", warn_last)):
        if days >= time_days:
            raise CleanupPolicyError(f"{label} باید کمتر از مهلت سرویس منقضی باشد")
    if warn_first and warn_last and warn_first <= warn_last:
        raise CleanupPolicyError("هشدار اول باید زودتر از هشدار آخر باشد")
    return CleanupPolicy(
        time_on=str(form.get("time_on") or "") == "1",
        time_days=time_days,
        warn_first=warn_first,
        warn_last=warn_last,
        volume_on=str(form.get("volume_on") or "") == "1",
        volume_days=volume_days,
    )


async def load_policy(session: AsyncSession, *, shop_id: int | None) -> CleanupPolicy:
    return policy_from_settings(await get_all_settings(session, reseller_id=shop_id))


async def save_policy(
    session: AsyncSession, *, shop_id: int | None, policy: CleanupPolicy, now: datetime | None = None
) -> CleanupPolicy:
    """Store the rule. The moment a rule is switched on is kept, so the grace period starts there."""
    now = now or datetime.now(timezone.utc)
    current = await load_policy(session, shop_id=shop_id)
    time_at = current.time_enabled_at if (policy.time_on and current.time_on and current.time_enabled_at) else (now if policy.time_on else None)
    volume_at = (
        current.volume_enabled_at
        if (policy.volume_on and current.volume_on and current.volume_enabled_at)
        else (now if policy.volume_on else None)
    )
    values = {
        KEY_TIME_ON: "1" if policy.time_on else "0",
        KEY_TIME_DAYS: str(policy.time_days),
        KEY_TIME_ENABLED_AT: time_at.isoformat() if time_at else "",
        KEY_WARN_FIRST: str(policy.warn_first),
        KEY_WARN_LAST: str(policy.warn_last),
        KEY_VOLUME_ON: "1" if policy.volume_on else "0",
        KEY_VOLUME_DAYS: str(policy.volume_days),
        KEY_VOLUME_ENABLED_AT: volume_at.isoformat() if volume_at else "",
    }
    await set_settings_bulk(session, values, reseller_id=shop_id)
    return policy_from_settings(values)


def shop_services_clause(shop_id: int | None) -> ColumnElement[bool]:
    """Services sold by one shop; the platform's own services have no shop."""
    return UserService.shop_id.is_(None) if shop_id is None else UserService.shop_id == int(shop_id)


def _eligible() -> list[ColumnElement[bool]]:
    return [
        UserService.pg_user_id.is_not(None),
        UserService.is_cancelled.is_(False),
        UserService.cancellation_pending.is_(False),
        UserService.shop_needs_review.is_(False),
        UserService.cleanup_exempt.is_(False),
    ]


def _not_held(now: datetime) -> ColumnElement[bool]:
    return or_(UserService.cleanup_hold_until.is_(None), UserService.cleanup_hold_until <= now)


def time_due_clause(policy: CleanupPolicy, now: datetime, *, ahead: timedelta = timedelta(0)) -> ColumnElement[bool] | None:
    """Expired services whose deadline (later of expiry and switch-on, plus the days) is within ``ahead``."""
    if not policy.time_on or policy.time_enabled_at is None:
        return None
    if policy.time_enabled_at + timedelta(days=policy.time_days) > now + ahead:
        return None
    cutoff = now + ahead - timedelta(days=policy.time_days)
    return and_(UserService.quota_expire_at.is_not(None), UserService.quota_expire_at < now, UserService.quota_expire_at <= cutoff)


def volume_due_clause(policy: CleanupPolicy, now: datetime, *, ahead: timedelta = timedelta(0)) -> ColumnElement[bool] | None:
    if not policy.volume_on or policy.volume_enabled_at is None:
        return None
    if policy.volume_enabled_at + timedelta(days=policy.volume_days) > now + ahead:
        return None
    cutoff = now + ahead - timedelta(days=policy.volume_days)
    return and_(UserService.volume_exhausted_at.is_not(None), UserService.volume_exhausted_at <= cutoff)


@dataclass(frozen=True, slots=True)
class PreviewRow:
    """One service that is already past its deadline or will be within the preview window."""

    service_id: int
    username: str
    customer: str
    kind: str
    since: str
    deadline: str


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    expired_total: int
    time_due_now: int
    time_due_soon: int
    volume_total: int
    volume_due_now: int
    volume_due_soon: int
    rows: list[PreviewRow]
    cache_note: str = "بر اساس آخرین اطلاعات ذخیره‌شدهٔ سرویس‌ها؛ پیش از هر حذف، وضعیت دوباره از پنل خوانده می‌شود."


async def _count(session: AsyncSession, shop_id: int | None, *clauses: ColumnElement[bool], now: datetime) -> int:
    query = select(func.count(UserService.id)).where(shop_services_clause(shop_id), *_eligible(), _not_held(now), *clauses)
    return int(await session.scalar(query) or 0)


def _local(moment: datetime | None) -> str:
    if moment is None:
        return ""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d")


async def build_preview(
    session: AsyncSession, *, shop_id: int | None, policy: CleanupPolicy, now: datetime | None = None
) -> CleanupPreview:
    """Counts and a short list from the cached quota columns; changes nothing and calls no panel."""
    now = now or datetime.now(timezone.utc)
    soon = timedelta(days=PREVIEW_SOON_DAYS)
    expired_total = await _count(session, shop_id, UserService.quota_expire_at.is_not(None), UserService.quota_expire_at < now, now=now)
    volume_total = await _count(session, shop_id, UserService.volume_exhausted_at.is_not(None), now=now)
    counts = {"time_due_now": 0, "time_due_soon": 0, "volume_due_now": 0, "volume_due_soon": 0}
    for prefix, make in (("time", time_due_clause), ("volume", volume_due_clause)):
        due_now = make(policy, now)
        due_soon = make(policy, now, ahead=soon)
        if due_now is not None:
            counts[f"{prefix}_due_now"] = await _count(session, shop_id, due_now, now=now)
        if due_soon is not None:
            counts[f"{prefix}_due_soon"] = await _count(session, shop_id, due_soon, now=now) - counts[f"{prefix}_due_now"]
    rows = await _preview_rows(session, shop_id, policy, now, soon)
    return CleanupPreview(expired_total, counts["time_due_now"], counts["time_due_soon"], volume_total,
                          counts["volume_due_now"], counts["volume_due_soon"], rows)


async def _preview_rows(
    session: AsyncSession, shop_id: int | None, policy: CleanupPolicy, now: datetime, soon: timedelta
) -> list[PreviewRow]:
    rows: list[PreviewRow] = []
    for make, column, days, enabled_at, label in (
        (time_due_clause, UserService.quota_expire_at, policy.time_days, policy.time_enabled_at, "انقضا"),
        (volume_due_clause, UserService.volume_exhausted_at, policy.volume_days, policy.volume_enabled_at, "اتمام حجم"),
    ):
        clause = make(policy, now, ahead=soon)
        if clause is None:
            continue
        found = (
            await session.execute(
                select(UserService.id, UserService.pg_username, column, BotUser.full_name, BotUser.username)
                .join(BotUser, BotUser.id == UserService.bot_user_id)
                .where(shop_services_clause(shop_id), *_eligible(), _not_held(now), clause)
                .order_by(column.asc(), UserService.id)
                .limit(PREVIEW_SAMPLE)
            )
        ).all()
        for service_id, username, since, full_name, handle in found:
            started = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            deadline = max(started, enabled_at or started) + timedelta(days=days)
            rows.append(
                PreviewRow(
                    service_id=int(service_id),
                    username=str(username or "")[:USERNAME_PREVIEW_MAX],
                    customer=str(full_name or (f"@{handle}" if handle else "—"))[:USERNAME_PREVIEW_MAX],
                    kind=label,
                    since=_local(since),
                    deadline=_local(deadline),
                )
            )
    return rows[:PREVIEW_SAMPLE]
