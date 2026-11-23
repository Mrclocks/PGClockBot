"""«کارهای گیرکرده» on the owner's health page: work that should have finished on its own and has not.

Each line counts one kind of unfinished work with one indexed query. When the job that finishes it can run
again, the owner asks for a run now: the scheduler runs that same job ahead of its timer, with its usual
one-instance rule and its own claims, so nothing is done twice and the request does not wait on PasarGuard or
Telegram. Failed deliveries have their own page with a retry per order, so their line links there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CashbackGrant,
    DeliveryFailure,
    PaymentSettlement,
    ServiceFreeze,
    ServiceTransfer,
    SettlementStatus,
)
from app.services.cashback import GrantStatus
from app.services.job_trigger import run_job_now
from app.services.numbers import to_fa_digits
from app.services.payment_settlement import CHANNEL_PSP, PSP_SETTLING_STALE_AFTER
from app.services.service_freeze_settings import STATUS_FROZEN, STATUS_PAUSING, STATUS_RESUMING
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.system_health import ERR, OK, WARN, HealthGroup, HealthItem

RERUN_PATH = "/tools/health/rerun"
# A job runs every minute or five; past this it is stuck, not just waiting for its next run.
STUCK_AFTER = timedelta(minutes=15)
NOTHING_STUCK = "چیزی نمانده"


@dataclass(frozen=True, slots=True)
class StuckKind:
    key: str
    label: str
    icon: str
    job_id: str
    tone_when_stuck: str


# The only jobs the page may run, keyed by the path segment of their button.
STUCK_KINDS: dict[str, StuckKind] = {
    "settlements": StuckKind("settlements", "پرداخت آنلاینِ مانده در تسویه", "card", "psp_settlement_poll", ERR),
    "freezes": StuckKind("freezes", "سرویس‌های مانده در توقف", "time", "service_freezes", WARN),
    "cashback": StuckKind("cashback", "کش‌بک‌های واریزنشده", "wallet", "cashback_mature", WARN),
    "transfers": StuckKind("transfers", "لینک‌های عوض‌نشده بعد از انتقال", "refresh", "service_transfers", WARN),
}
DELIVERIES = HealthItem("deliveries", "تحویل‌های ناموفق", "alert", OK, NOTHING_STUCK, "/finance?tab=delivery")


async def _count(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    return int(await session.scalar(statement) or 0)


async def stuck_counts(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """How many items of each kind are stuck at ``now``."""
    now = now or datetime.now(timezone.utc)
    stale = now - STUCK_AFTER
    return {
        "settlements": await _count(session, select(func.count(PaymentSettlement.id)).where(
            PaymentSettlement.channel == CHANNEL_PSP,
            PaymentSettlement.status == SettlementStatus.SETTLING.value,
            PaymentSettlement.updated_at < now - PSP_SETTLING_STALE_AFTER,
        )),
        "deliveries": await _count(session, select(func.count(DeliveryFailure.id)).where(
            DeliveryFailure.resolved_at.is_(None),
        )),
        "freezes": await _count(session, select(func.count(ServiceFreeze.id)).where(or_(
            ServiceFreeze.status.in_((STATUS_PAUSING, STATUS_RESUMING)),
            (ServiceFreeze.status == STATUS_FROZEN) & (ServiceFreeze.resume_by < stale),
        ))),
        "cashback": await _count(session, select(func.count(CashbackGrant.id)).where(
            CashbackGrant.status == str(GrantStatus.PENDING), CashbackGrant.available_at < stale,
        )),
        "transfers": await _count(session, select(func.count(ServiceTransfer.id)).where(
            ServiceTransfer.link_rotation_pending.is_(True), ServiceTransfer.accepted_at < stale,
        )),
    }


def _line(kind: StuckKind, count: int) -> HealthItem:
    if not count:
        return HealthItem(kind.key, kind.label, kind.icon, OK, NOTHING_STUCK)
    summary = f"{to_fa_digits(count)} مورد؛ برای اجرای دوباره بزنید"
    return HealthItem(kind.key, kind.label, kind.icon, kind.tone_when_stuck, summary, action=f"{RERUN_PATH}/{kind.key}")


async def stuck_group(session: AsyncSession) -> HealthGroup:
    """The «کارهای گیرکرده» lines, in page order."""
    counts = await stuck_counts(session)
    deliveries = counts["deliveries"]
    delivery_line = DELIVERIES if not deliveries else HealthItem(
        DELIVERIES.key, DELIVERIES.label, DELIVERIES.icon, ERR,
        f"{to_fa_digits(deliveries)} سفارش پرداخت‌شده تحویل نشده", DELIVERIES.href,
    )
    lines = [_line(STUCK_KINDS["settlements"], counts["settlements"]), delivery_line]
    lines += [_line(STUCK_KINDS[key], counts[key]) for key in ("freezes", "cashback", "transfers")]
    return HealthGroup("کارهای گیرکرده", tuple(lines))


async def request_rerun(session: AsyncSession, staff: dict, key: str) -> StuckKind:
    """Run the job behind ``key`` now and record who asked; ValueError for an unknown key or a stopped scheduler."""
    kind = STUCK_KINDS.get(key)
    if kind is None:
        raise ValueError("این کار شناخته نشد")
    if not run_job_now(kind.job_id):
        raise ValueError("زمان‌بند کارها روشن نیست؛ پنل را دوباره راه‌اندازی کنید")
    add_staff_action(session, actor_from_staff(staff, shop_id=None), action="job_rerun", target_type="job",
                     target_id=None, reason=kind.label)
    await session.commit()
    return kind
