"""Switching a reseller between fixed and PAYG billing.

A reseller has exactly one billing mode. Changing it is an explicit, confirmed
action that also resets everything tied to the previous mode: PAYG usage starts
from the admin's current traffic, and a PAYG suspension or warning state never
survives a move to a fixed plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerPlan, ResellerProfile
from app.services.billing import (
    BILLING_MODE_FIXED,
    BILLING_MODE_PAYG,
    PaygRateError,
    ensure_billing_enabled,
    get_traffic_source,
    require_billable_payg_plan,
    rate_context_for_profile,
    resolve_price_per_gb,
)

from app.services.payg_pricing import BASIS_USER, rates_from_plan

logger = logging.getLogger(__name__)

BILLING_MODES = frozenset({BILLING_MODE_FIXED, BILLING_MODE_PAYG})
_MODE_LABELS = {BILLING_MODE_FIXED: "ثابت", BILLING_MODE_PAYG: "PAYG"}


class BillingModeChangeError(ValueError):
    """A billing mode change was refused; the message is safe to show to an admin."""


@dataclass(frozen=True, slots=True)
class ModeChange:
    previous: str
    new: str

    @property
    def changed(self) -> bool:
        return self.previous != self.new


def normalize_mode(raw: str | None) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in BILLING_MODES else BILLING_MODE_FIXED


def assert_mode_change_confirmed(
    previous_mode: str | None, new_mode: str, *, confirmed: bool
) -> None:
    """Refuse to change an active reseller's mode unless the admin confirmed it."""
    previous = normalize_mode(previous_mode)
    if previous == new_mode or confirmed:
        return
    raise BillingModeChangeError(
        f"این نماینده در حالت «{_MODE_LABELS[previous]}» است؛ برای تغییر به "
        f"«{_MODE_LABELS[new_mode]}» گزینهٔ «تأیید تغییر نوع پرداخت» را فعال کنید"
    )


async def read_admin_traffic_bytes(pg_username: str) -> int:
    """Current lifetime traffic of a PasarGuard admin, used as the PAYG starting point."""
    from app.services.pasarguard import get_pg

    gate, admin = await get_pg().get_admin_gate(pg_username)
    if gate == "missing":
        raise BillingModeChangeError("ادمین پاسارگارد این نماینده در پنل پیدا نشد")
    if not isinstance(admin, dict):
        raise BillingModeChangeError(
            "پاسارگارد پاسخ نداد؛ برای تنظیم نقطهٔ شروع مصرف PAYG کمی بعد دوباره تلاش کنید"
        )
    used = get_traffic_source().extract_bytes(admin)
    if used is None:
        raise BillingModeChangeError("مصرف ترافیک این ادمین از پاسارگارد خوانده نشد")
    return used


async def starting_watermark(pg_username: str | None) -> int:
    """Traffic already used by the admin, so the first PAYG tick bills only new usage."""
    uname = (pg_username or "").strip()
    return await read_admin_traffic_bytes(uname) if uname else 0


def apply_mode_fields(
    profile: ResellerProfile, new_mode: str, *, watermark_bytes: int | None = None
) -> None:
    """Set the mode and clear billing state that belonged to the other mode."""
    profile.billing_mode = new_mode
    profile.billing_low_warned_at = None
    profile.billing_low_warned_level = None
    profile.billing_tick_issue = None
    if new_mode == BILLING_MODE_PAYG:
        profile.billing_watermark_bytes = int(watermark_bytes or 0)


async def finish_mode_change(
    session: AsyncSession, profile: ResellerProfile, change: ModeChange
) -> None:
    """Run the network-facing follow-ups once the new mode is committed."""
    if change.new == BILLING_MODE_PAYG:
        await ensure_billing_enabled(session)
        return
    if change.changed and profile.billing_suspended_at is not None:
        from app.services.billing_suspend import restore_payg_reseller

        await restore_payg_reseller(session, profile, commit=True, force=True)


async def _assert_shop_billable(session: AsyncSession, profile: ResellerProfile) -> None:
    """A shop moved to PAYG must have a package that bills it (per-GB rate or user price)."""
    plan = await session.get(ResellerPlan, int(profile.plan_id)) if profile.plan_id else None
    if plan is not None and str(getattr(plan, "billing_mode", "") or "").lower() == BILLING_MODE_PAYG:
        require_billable_payg_plan(plan)
        if rates_from_plan(plan).basis == BASIS_USER:
            return
    rate = await resolve_price_per_gb(session, rate_context_for_profile(profile))
    if rate <= 0:
        raise PaygRateError(
            "برای این نماینده نرخ هر گیگ تعریف نشده؛ پلن PAYG با نرخ مثبت را از "
            "«اعطای نماینده» به او بدهید"
        )


async def switch_billing_mode(
    session: AsyncSession,
    profile: ResellerProfile,
    new_mode: str,
    *,
    confirmed: bool,
) -> ModeChange:
    """Change an existing reseller's billing mode after confirmation."""
    target = normalize_mode(new_mode)
    change = ModeChange(previous=normalize_mode(profile.billing_mode), new=target)
    if not change.changed:
        return change
    assert_mode_change_confirmed(change.previous, target, confirmed=confirmed)

    watermark: int | None = None
    if target == BILLING_MODE_PAYG:
        await _assert_shop_billable(session, profile)
        watermark = await starting_watermark(profile.pg_admin_username)

    apply_mode_fields(profile, target, watermark_bytes=watermark)
    await session.commit()
    await finish_mode_change(session, profile, change)
    return change
