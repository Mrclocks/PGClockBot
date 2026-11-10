"""PAYG charges at the points where a shop's panel user is created or changed.

Callers use these next to their volume hold, in the same order:

1. ``charge_shop_change`` right before the panel call (commits the debit).
2. The panel call.
3. ``finish_charge`` on success (may join the caller's commit), or
   ``settle_failed_charge`` on failure: a refund only when nothing reached the panel or
   the panel definitely refused; any other failure goes to review and keeps the money.

Platform orders (no shop) and fixed-package shops are never charged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerCharge, ResellerProfile
from app.services.billing import SHORTFALL_MESSAGE, WalletShortfall, is_payg, payg_spendable_for_charges
from app.services.billing_alerts import TickIssue, announce_wallet_short, clear_tick_issue, notify_credit_use
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman, parse_expire
from app.services.payg_charges import (
    ChargeRequest,
    PARENT_SHORTFALL_MESSAGE,
    ParentWalletShortfall,
    new_request_key,
    parent_share,
    quote_for_shop,
    refund_charge,
    reserve_charge,
    send_to_review,
    settle_charge,
    shop_rates,
)
from app.services.child_pricing import parent_shop_profile
from app.services.payg_pricing import (
    RESET_EXPLANATION,
    ChargeEvent,
    ChargeQuote,
    PaygPriceError,
    ServiceChange,
    child_plan_id_of,
)
from app.services.provision_gate import ProvisionError, reseller_id_from_staff
from app.services.credit_statements import refusal_for, shop_restriction
from app.services.shop_credit import claim_credit_alert, credit_usage, shop_credit_limit
from app.services.volume_allocation import HeldVolume, is_hard_panel_rejection, release_volume_hold

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400
REFUND_REASON = "پاسخ قطعی پنل: تغییر انجام نشد"
TEMPLATE_UNREADABLE_MESSAGE = (
    "حجم و مدت قالب این پلن از پاسارگارد خوانده نشد؛ برای محاسبهٔ هزینه کمی بعد دوباره تلاش کنید"
)
# The panel's own periodic reset looks the same as a manual one, so reconciliation charges both.
AUTO_RESET_NOTE = "کاربری که در پاسارگارد ریست خودکار دارد، هر بار ریست هزینهٔ کل حجم دارد"
PARENT_OVERDUE_MESSAGE = "فروش موقتاً متوقف است؛ نمایندهٔ بالادست شما بدهی سررسیدگذشته دارد"
SHORTFALL_FOR_CUSTOMER = "این سرویس فعلاً قابل تحویل نیست؛ کمی بعد دوباره تلاش کنید"


async def shop_profile(session: AsyncSession, shop_id: int | None) -> ResellerProfile | None:
    if not shop_id:
        return None
    return await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))


async def quote_shop_change(
    session: AsyncSession, shop_id: int | None, change: ServiceChange, *, quantity: int = 1
) -> ChargeQuote | None:
    """The price to show before confirming; ``None`` when this change costs the shop nothing."""
    profile = await shop_profile(session, shop_id)
    if profile is None or not is_payg(profile):
        return None
    try:
        quote = await quote_for_shop(session, profile, change)
        return None if quote is None or quote.is_free else quote.for_quantity(quantity)
    except PaygPriceError as exc:
        raise ProvisionError(str(exc)) from exc


async def shop_charges_at_creation(session: AsyncSession, shop_id: int | None) -> bool:
    """Whether this shop pays when a user is created or changed (skips panel reads otherwise)."""
    profile = await shop_profile(session, shop_id)
    if profile is None or not is_payg(profile):
        return False
    try:
        rates = await shop_rates(session, profile)
    except PaygPriceError:
        return True  # the charge itself refuses the change with the safe message
    return rates is not None and (rates.charges_traffic or rates.charges_users)


async def charge_shop_change(
    session: AsyncSession,
    *,
    shop_id: int | None,
    change: ServiceChange,
    request_key: str,
    pg_username: str,
    panel_target: Mapping[str, Any] | None = None,
    quantity: int = 1,
    expected_amount: int | None = None,
    created_by: str | None = None,
    ladder_exempt: bool = False,
) -> ResellerCharge | None:
    """Take the shop's PAYG charge for ``change`` before the panel call; raises ``ProvisionError``.

    ``ladder_exempt`` is for changes that already happened in the panel (reconciliation):
    the overdue ladder may not refuse a charge for something the shop already took.
    """
    profile = await shop_profile(session, shop_id)
    if profile is None or not is_payg(profile):
        return None
    if not ladder_exempt:
        await _assert_ladder_allows(session, profile, change)
    request = ChargeRequest(
        request_key=request_key,
        change=change,
        pg_username=pg_username,
        panel_target=dict(panel_target or {}),
        quantity=quantity,
        expected_amount=expected_amount,
        created_by=created_by,
    )
    try:
        charge = await reserve_charge(session, profile, request)
    except ParentWalletShortfall as exc:
        await _announce_short_safely(session, exc.parent_profile)
        raise ProvisionError(str(exc)) from exc
    except WalletShortfall as exc:
        await _announce_short_safely(session, profile)
        raise ProvisionError(str(exc)) from exc
    except ValueError as exc:
        raise ProvisionError(str(exc)) from exc
    if charge is not None:
        await clear_tick_issue(session, profile, (TickIssue.WALLET_SHORT,))
        await _alert_credit_use(session, profile)
    return charge


async def _alert_credit_use(session: AsyncSession, profile: ResellerProfile) -> None:
    """After a charge: tell the shop once per level how much of its credit line is used."""
    shop_id = int(profile.user_id)
    try:
        limit = await shop_credit_limit(session, shop_id)
        if limit <= 0:
            return
        balance = int(profile.billing_balance or 0)
        level = await claim_credit_alert(session, shop_id, balance=balance, limit=limit)
        if level is not None:
            used = credit_usage(balance, limit).used
            await notify_credit_use(session, profile, level=level, used=used, limit=limit)
    except Exception:  # noqa: BLE001 - optional alert after a committed charge; never undoes the sale
        logger.warning("credit alert failed shop=%s", shop_id, exc_info=True)
        await rollback_quiet(session)


async def _assert_ladder_allows(session: AsyncSession, profile: ResellerProfile, change: ServiceChange) -> None:
    """The overdue ladder of the shop, and of the level-1 parent that pays half of a level-2 charge."""
    refusal = refusal_for(await shop_restriction(session, int(profile.user_id)), change.event)
    if refusal is not None:
        raise ProvisionError(refusal)
    if child_plan_id_of(profile) is None:
        return
    parent = await parent_shop_profile(session, profile)
    if parent is not None and refusal_for(await shop_restriction(session, int(parent.user_id)), change.event):
        raise ProvisionError(PARENT_OVERDUE_MESSAGE)


async def _announce_short_safely(session: AsyncSession, profile: ResellerProfile) -> None:
    """Best effort (sends already log their own failures): a failed alert never hides the refusal."""
    shop_id = int(profile.user_id)
    try:
        await announce_wallet_short(session, profile)
    except Exception:  # noqa: BLE001 - optional alert; the caller raises the refusal right after
        logger.warning("wallet short alert failed shop=%s", shop_id, exc_info=True)
        await rollback_quiet(session)


async def assert_shop_can_afford(
    session: AsyncSession, shop_id: int | None, change: ServiceChange, *, quantity: int = 1
) -> None:
    """Checkout check: refuse a sale the shop cannot pay for, before the buyer pays.

    Not a reservation; delivery still charges atomically. Raises ``ProvisionError``.
    """
    profile = await shop_profile(session, shop_id)
    if profile is None or not is_payg(profile):
        return
    await _assert_ladder_allows(session, profile, change)
    quote = await quote_shop_change(session, shop_id, change, quantity=quantity)
    if quote is None:
        return
    if await payg_spendable_for_charges(session, profile) < quote.amount:
        await _announce_short_safely(session, profile)
        raise ProvisionError(SHORTFALL_MESSAGE)
    await _assert_parent_affordable(session, profile, change, quantity=quantity)


async def _assert_parent_affordable(
    session: AsyncSession, profile: ResellerProfile, change: ServiceChange, *, quantity: int
) -> None:
    """A level-2 sale also needs its parent's half; a short parent is told, not the child."""
    try:
        share = await parent_share(session, profile, change, quantity=quantity)
    except PaygPriceError as exc:
        raise ProvisionError(str(exc)) from exc
    if share is None:
        return
    parent_profile, parent_quote = share
    if await payg_spendable_for_charges(session, parent_profile) >= parent_quote.amount:
        return
    await _announce_short_safely(session, parent_profile)
    raise ProvisionError(PARENT_SHORTFALL_MESSAGE)


async def finish_charge(session: AsyncSession, charge: ResellerCharge | None, *, commit: bool = True) -> None:
    """The panel applied the change."""
    if charge is not None:
        await settle_charge(session, charge, commit=commit)


async def settle_failed_charge(
    session: AsyncSession, charge: ResellerCharge | None, exc: BaseException, *, panel_touched: bool
) -> None:
    """After a failure: refund only when the panel cannot have applied the change."""
    if charge is None:
        return
    try:
        if not panel_touched or is_hard_panel_rejection(exc):
            await refund_charge(session, charge, reason=REFUND_REASON)
        else:
            await send_to_review(session, charge)
    except SQLAlchemyError:
        # The charge stays pending with its money held; it is listed for review, never refunded blindly.
        logger.warning("payg charge settle after failure did not commit charge=%s", charge.id, exc_info=True)
        await rollback_quiet(session)


def template_volume(template: Any) -> tuple[int, int]:
    """``(data_limit bytes, days)`` of a PasarGuard user template; 0 means unlimited.

    Raises ``ProvisionError`` when the template does not say, so an unknown volume is never sold.
    """
    if not isinstance(template, dict):
        raise ProvisionError(TEMPLATE_UNREADABLE_MESSAGE)
    limit = template.get("data_limit")
    duration = template.get("expire_duration")
    if limit is None:
        limit = 0
    if duration is None:
        duration = 0
    for value in (limit, duration):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProvisionError(TEMPLATE_UNREADABLE_MESSAGE)
    return int(limit), -(-int(duration) // SECONDS_PER_DAY)


async def read_template_volume(pg: Any, template_id: int) -> tuple[int, int]:
    try:
        template = await pg.get_user_template(int(template_id))
    except Exception as exc:  # noqa: BLE001 - any read failure means the price is unknown; fail closed
        logger.warning("template read for payg failed template=%s", template_id, exc_info=True)
        raise ProvisionError(TEMPLATE_UNREADABLE_MESSAGE) from exc
    return template_volume(template)


def added_days(old_expire_ts: int | None, new_expire_ts: int | None, *, now_ts: int) -> int | None:
    """Days a new expiry adds; ``None`` makes the period unlimited, 0 adds nothing.

    An already unlimited user gains nothing from a finite expiry. An expired one counts
    from now, so reviving it is charged in full.
    """
    if not new_expire_ts:
        return None if old_expire_ts else 0
    if not old_expire_ts:
        return 0
    base = max(int(old_expire_ts), int(now_ts))
    return max(0, -(-(int(new_expire_ts) - base) // SECONDS_PER_DAY))


def service_edit_changes(
    *,
    renew: bool,
    current_limit: int | None,
    new_limit: int | None,
    current_expire: int | None,
    new_expire: int | None,
    reset: bool,
    now_ts: int,
) -> list[ServiceChange]:
    """Changes a staff edit of one user sells; they never count the same volume or day twice.

    ``new_limit``/``new_expire`` ``None`` leave that part unchanged; ``0`` makes it unlimited.
    A renew is one new period (its volume and user price again); any other edit is at most
    a traffic part (a reset sells the whole volume, otherwise only an increase) and a time part.
    """
    limit_after = int(current_limit or 0) if new_limit is None else int(new_limit)
    if renew:
        days: int | None = 0
        if new_expire:
            days = max(1, -(-(int(new_expire) - int(now_ts)) // SECONDS_PER_DAY))
        return [ServiceChange(ChargeEvent.RENEW, data_limit_bytes=limit_after, days=days)]
    changes: list[ServiceChange] = []
    if reset:
        changes.append(ServiceChange(ChargeEvent.RESET, data_limit_bytes=limit_after))
    elif new_limit is not None:
        changes.append(ServiceChange(
            ChargeEvent.ADD_VOLUME, data_limit_bytes=int(new_limit), previous_limit_bytes=int(current_limit or 0)
        ))
    if new_expire is not None:
        changes.append(ServiceChange(
            ChargeEvent.ADD_DAYS, days=added_days(current_expire, new_expire, now_ts=now_ts)
        ))
    return changes


async def charge_shop_changes(
    session: AsyncSession,
    *,
    shop_id: int | None,
    changes: list[ServiceChange],
    request_key: str,
    pg_username: str,
    panel_target: Mapping[str, Any] | None = None,
    created_by: str | None = None,
) -> list[ResellerCharge]:
    """Charge each part of one edit; if a later part is refused, the earlier ones are refunded."""
    taken: list[ResellerCharge] = []
    for index, change in enumerate(changes):
        try:
            charge = await charge_shop_change(
                session, shop_id=shop_id, change=change, request_key=f"{request_key}:{index}",
                pg_username=pg_username, panel_target=panel_target, created_by=created_by,
            )
        except ProvisionError:
            for earlier in taken:
                await refund_charge(session, earlier, reason="بخش دیگر همین تغییر پذیرفته نشد")
            raise
        if charge is not None:
            taken.append(charge)
    return taken


async def finish_charges(session: AsyncSession, charges: list[ResellerCharge], *, commit: bool = True) -> None:
    for charge in charges:
        await settle_charge(session, charge, commit=False)
    if commit and charges:
        await session.commit()


async def settle_failed_charges(
    session: AsyncSession, charges: list[ResellerCharge], exc: BaseException, *, panel_touched: bool
) -> None:
    for charge in charges:
        await settle_failed_charge(session, charge, exc, panel_touched=panel_touched)


def _days_from_now(expire_ts: int | None, *, now_ts: int) -> int:
    """Period length of a new user expiring at ``expire_ts``; 0 is unlimited."""
    if not expire_ts:
        return 0
    return max(1, -(-(int(expire_ts) - int(now_ts)) // SECONDS_PER_DAY))


async def _free_hold(session: AsyncSession, hold: HeldVolume | None) -> None:
    await release_volume_hold(session, hold)
    await session.commit()


async def charge_staff_create(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    *,
    pg: Any,
    hold: HeldVolume | None,
    template_id: int | None,
    data_limit: int | None,
    expire_ts: int | None,
    username: str,
    now_ts: int,
) -> ResellerCharge | None:
    """Charge a create from the PasarGuard pages to the staff's shop; frees the hold when refused."""
    shop_id = reseller_id_from_staff(dict(staff or {}))
    if not await shop_charges_at_creation(session, shop_id):
        return None
    try:
        limit, days = int(data_limit or 0), _days_from_now(expire_ts, now_ts=now_ts)
        if template_id:
            limit, days = await read_template_volume(pg, int(template_id))
        return await charge_shop_change(
            session, shop_id=shop_id,
            change=ServiceChange(ChargeEvent.CREATE, data_limit_bytes=limit, days=days),
            request_key=f"pg-create:{new_request_key()}", pg_username=username,
            panel_target={"username": username, "template_id": template_id}, created_by="pg-page",
        )
    except ProvisionError:
        await _free_hold(session, hold)
        raise


def _live_expire_ts(user: Mapping[str, Any]) -> int | None:
    expire = parse_expire(user.get("expire") or user.get("expire_date"))
    return int(expire.timestamp()) if expire else None


async def charge_staff_edit(
    session: AsyncSession,
    staff: Mapping[str, Any] | None,
    *,
    hold: HeldVolume | None,
    current: Mapping[str, Any],
    data_limit: int | None,
    expire_ts: int | None,
    reset: bool = False,
    now_ts: int,
) -> list[ResellerCharge]:
    """Charge an edit from the PasarGuard pages from the user's live values; frees the hold when refused."""
    shop_id = reseller_id_from_staff(dict(staff or {}))
    if not await shop_charges_at_creation(session, shop_id):
        return []
    changes = service_edit_changes(
        renew=False,
        current_limit=int(current.get("data_limit") or 0),
        new_limit=data_limit,
        current_expire=_live_expire_ts(current),
        new_expire=expire_ts,
        reset=reset,
        now_ts=now_ts,
    )
    try:
        return await charge_shop_changes(
            session, shop_id=shop_id, changes=changes, request_key=f"pg-edit:{new_request_key()}",
            pg_username=str(current.get("username") or ""), panel_target={"pg_user_id": current.get("id")},
            created_by="pg-page",
        )
    except ProvisionError:
        await _free_hold(session, hold)
        raise


async def charge_staff_reset(
    session: AsyncSession, staff: Mapping[str, Any] | None, *, pg: Any, pg_user_id: int, now_ts: int
) -> list[ResellerCharge]:
    """Charge a traffic reset from the PasarGuard pages: the user's whole volume becomes usable again."""
    if not await shop_charges_at_creation(session, reseller_id_from_staff(dict(staff or {}))):
        return []
    current = await pg.get_user_by_id(int(pg_user_id))
    if not isinstance(current, dict):
        raise ProvisionError("کاربر پاسارگارد یافت نشد")
    return await charge_staff_edit(
        session, staff, hold=None, current=current, data_limit=None, expire_ts=None, reset=True, now_ts=now_ts
    )


async def reset_with_charge(
    session: AsyncSession, staff: Mapping[str, Any] | None, *, pg: Any, pg_user_id: int, now_ts: int
) -> Any:
    """Reset a user's traffic from the PasarGuard pages, charging its PAYG shop first."""
    charges = await charge_staff_reset(session, staff, pg=pg, pg_user_id=pg_user_id, now_ts=now_ts)
    try:
        result = await pg.reset_user_by_id(int(pg_user_id))
    except Exception as exc:
        await settle_failed_charges(session, charges, exc, panel_touched=True)
        raise
    await finish_charges(session, charges)
    return result


@dataclass(frozen=True, slots=True)
class RateSummary:
    """The shop's PAYG prices as staff see them before creating or changing a user."""

    lines: tuple[str, ...]
    charges_reset: bool

    @property
    def reset_note(self) -> str:
        return RESET_EXPLANATION if self.charges_reset else ""


async def shop_rate_summary(session: AsyncSession, shop_id: int | None) -> RateSummary | None:
    """``None`` when the shop is not charged when users are created or changed."""
    profile = await shop_profile(session, shop_id)
    if profile is None or not is_payg(profile):
        return None
    try:
        rates = await shop_rates(session, profile)
    except PaygPriceError as exc:
        return RateSummary(lines=(str(exc),), charges_reset=False)
    if rates is None or not (rates.charges_traffic or rates.charges_users):
        return None
    lines: list[str] = []
    if rates.charges_traffic:
        lines.append(f"هر گیگ حجمی که روی کاربر زده می‌شود: {format_toman(rates.price_per_gb)}")
    if rates.charges_users:
        unit = "به ازای هر ۳۰ روز" if rates.user_price_per_30d else "برای هر کاربر"
        lines.append(f"قیمت کاربر ({unit}): {format_toman(rates.user_price)}")
    if rates.unlimited_price is None:
        lines.append("کاربر نامحدود: مجاز نیست")
    else:
        lines.append(f"کاربر نامحدود: {format_toman(rates.unlimited_price)}")
    if rates.charges_traffic:
        lines.append(AUTO_RESET_NOTE)
    return RateSummary(lines=tuple(lines), charges_reset=rates.charges_traffic)


def charged_note(charges: ResellerCharge | list[ResellerCharge] | None) -> str:
    """« — ۵۰٬۰۰۰ تومان از کیف پول کسر شد» for a success message; empty when nothing was charged."""
    items = [charges] if isinstance(charges, ResellerCharge) else list(charges or [])
    total = sum(int(c.amount) for c in items)
    return f" — {format_toman(total)} از کیف پول کسر شد" if total else ""
