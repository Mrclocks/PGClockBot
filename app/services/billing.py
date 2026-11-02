"""Unified reseller Billing service.

Pay As You Go is one ``billing_mode``; fixed is the other package type.
All money movement for PAYG must go through this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerBillingRate, ResellerBillingTransaction, ResellerProfile
from app.services.billing_alerts import (
    PANEL_TICK_ISSUES,
    TickIssue,
    claim_low_balance_warning,
    clear_tick_issue,
    notify_low_balance,
    report_tick_issue,
)

logger = logging.getLogger(__name__)

GB = 1024**3

BILLING_MODE_FIXED = "fixed"
BILLING_MODE_PAYG = "payg"

KIND_TOPUP = "topup"
KIND_USAGE = "usage"
KIND_ADJUSTMENT = "adjustment"

ON_EMPTY_BLOCK_PROVISION = "block_provision"

# Platform Setting keys (never hardcode prices in call sites)
SETTING_ENABLED = "billing_enabled"
SETTING_PRICE_PER_GB = "billing_price_per_gb"
SETTING_LOW_BALANCE = "billing_low_balance"
SETTING_ON_EMPTY = "billing_on_empty"
SETTING_TICK_MINUTES = "billing_tick_minutes"


class BillingError(Exception):
    """Raised when a billing policy blocks an operation."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class PaygRateError(ValueError):
    """A PAYG plan has no positive per-GB rate."""


PAYG_RATE_REQUIRED_MESSAGE = (
    "نرخ هر گیگ پلن PAYG باید بیشتر از صفر باشد؛ با نرخ صفر مصرف نماینده رایگان می‌شود"
)


def require_positive_payg_rate(price_per_gb: int) -> int:
    """Return the rate, or raise ValueError when a PAYG plan would bill nothing."""
    rate = int(price_per_gb or 0)
    if rate <= 0:
        raise PaygRateError(PAYG_RATE_REQUIRED_MESSAGE)
    return rate


# ---------------------------------------------------------------------------
# Traffic source (pluggable — prefer lifetime_used_traffic)
# ---------------------------------------------------------------------------


class TrafficSource(Protocol):
    """Extract billed bytes from a PasarGuard admin (or future) payload."""

    def extract_bytes(self, payload: dict[str, Any]) -> int | None: ...


class PasarGuardAdminTrafficSource:
    """Default PG admin traffic reader.

    Priority: ``lifetime_used_traffic`` → ``used_traffic`` / ``traffic_used``.
    Swap this class (or register another) when PG exposes a cleaner field.
    """

    PREFERRED_KEYS = ("lifetime_used_traffic",)
    FALLBACK_KEYS = ("used_traffic", "traffic_used")

    def extract_bytes(self, payload: dict[str, Any]) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in self.PREFERRED_KEYS:
            val = _as_nonneg_int(payload.get(key))
            if val is not None:
                return val
        for key in self.FALLBACK_KEYS:
            val = _as_nonneg_int(payload.get(key))
            if val is not None:
                return val
        return None


_DEFAULT_TRAFFIC_SOURCE: TrafficSource = PasarGuardAdminTrafficSource()


def get_traffic_source() -> TrafficSource:
    """Return the active traffic source (single seam for future PG APIs)."""
    return _DEFAULT_TRAFFIC_SOURCE


def set_traffic_source(source: TrafficSource) -> None:
    """Tests / future adapters may replace the default source."""
    global _DEFAULT_TRAFFIC_SOURCE
    _DEFAULT_TRAFFIC_SOURCE = source


def _as_nonneg_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Rate resolver (extensible; MVP UI uses global Setting only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateContext:
    """Context for resolving price/GB. Extra scopes ready for future rates."""

    reseller_user_id: int | None = None
    plan_id: int | None = None
    inbound_id: str | None = None
    node_id: str | None = None


SCOPE_PRIORITY = (
    ("node", "node_id"),
    ("inbound", "inbound_id"),
    ("plan", "plan_id"),
    ("reseller", None),
)


async def resolve_price_per_gb(session: AsyncSession, ctx: RateContext | None = None) -> int:
    """Resolve toman/GB. Rates table overrides win by specificity; else plan; else Setting."""
    ctx = ctx or RateContext()
    rows = (
        await session.execute(
            select(ResellerBillingRate).where(ResellerBillingRate.is_active.is_(True))
        )
    ).scalars().all()

    def _match(scope_kind: str, scope_key: str, reseller_user_id: int | None) -> int | None:
        for r in rows:
            if (r.scope_kind or "") != scope_kind:
                continue
            if (r.scope_key or "") != (scope_key or ""):
                continue
            rid = r.reseller_user_id
            if reseller_user_id is None:
                if rid is not None:
                    continue
            else:
                if rid is not None and int(rid) != int(reseller_user_id):
                    continue
                # reseller-scoped row must target this reseller
                if scope_kind == "reseller" and rid is None:
                    continue
            return max(0, int(r.price_per_gb or 0))
        return None

    rid = int(ctx.reseller_user_id) if ctx.reseller_user_id else None

    if ctx.node_id:
        hit = _match("node", str(ctx.node_id), rid)
        if hit is not None:
            return hit
        hit = _match("node", str(ctx.node_id), None)
        if hit is not None:
            return hit

    if ctx.inbound_id:
        hit = _match("inbound", str(ctx.inbound_id), rid)
        if hit is not None:
            return hit
        hit = _match("inbound", str(ctx.inbound_id), None)
        if hit is not None:
            return hit

    if ctx.plan_id is not None:
        hit = _match("plan", str(int(ctx.plan_id)), rid)
        if hit is not None:
            return hit
        hit = _match("plan", str(int(ctx.plan_id)), None)
        if hit is not None:
            return hit
        # Fallback: read ResellerPlan.price_per_gb directly (PAYG packages)
        from app.db.models import ResellerPlan

        plan = await session.get(ResellerPlan, int(ctx.plan_id))
        if plan is not None:
            plan_rate = max(0, int(getattr(plan, "price_per_gb", 0) or 0))
            if plan_rate > 0:
                return plan_rate

    if rid is not None:
        hit = _match("reseller", "", rid)
        if hit is not None:
            return hit

    hit = _match("default", "", None)
    if hit is not None:
        return hit

    # Global Setting fallback removed from product UI — rate lives on PAYG plans.
    # Keep a silent Setting read only as last-resort for legacy installs (default 0).
    from app.services.users import get_setting

    raw = await get_setting(session, SETTING_PRICE_PER_GB, "0")
    return max(0, _as_int(raw, 0))


async def sync_plan_billing_rate(session: AsyncSession, plan) -> None:
    """Upsert/deactivate global plan-scoped rate from a ResellerPlan PAYG package."""
    plan_id = int(getattr(plan, "id", 0) or 0)
    if plan_id <= 0:
        return
    mode = str(getattr(plan, "billing_mode", "") or "").strip().lower()
    price = max(0, int(getattr(plan, "price_per_gb", 0) or 0))
    active = bool(getattr(plan, "is_active", True)) and mode == BILLING_MODE_PAYG and price > 0

    row = (
        await session.execute(
            select(ResellerBillingRate).where(
                ResellerBillingRate.scope_kind == "plan",
                ResellerBillingRate.scope_key == str(plan_id),
                ResellerBillingRate.reseller_user_id.is_(None),
            )
        )
    ).scalar_one_or_none()

    if not active:
        if row is not None:
            row.is_active = False
            row.price_per_gb = 0
        return

    if row is None:
        session.add(
            ResellerBillingRate(
                scope_kind="plan",
                scope_key=str(plan_id),
                reseller_user_id=None,
                price_per_gb=price,
                is_active=True,
            )
        )
    else:
        row.price_per_gb = price
        row.is_active = True


async def delete_plan_billing_rate(session: AsyncSession, plan_id: int) -> None:
    rows = (
        await session.execute(
            select(ResellerBillingRate).where(
                ResellerBillingRate.scope_kind == "plan",
                ResellerBillingRate.scope_key == str(int(plan_id)),
            )
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)


def rate_context_for_profile(profile: ResellerProfile | None) -> RateContext:
    """Build RateContext so PAYG plan package rates apply."""
    if profile is None:
        return RateContext()
    plan_id = getattr(profile, "plan_id", None)
    try:
        pid = int(plan_id) if plan_id is not None else None
    except (TypeError, ValueError):
        pid = None
    if pid is not None and pid <= 0:
        pid = None
    return RateContext(
        reseller_user_id=int(profile.user_id) if profile.user_id else None,
        plan_id=pid,
    )


def bytes_to_charge_toman(bytes_delta: int, price_per_gb: int) -> int:
    """Ceil charge for consumed bytes at price_per_gb (toman)."""
    if bytes_delta <= 0 or price_per_gb <= 0:
        return 0
    # ceil(bytes / GB) * price
    gb_units = (int(bytes_delta) + GB - 1) // GB
    return int(gb_units) * int(price_per_gb)


def bytes_cost_proportional(bytes_delta: int, price_per_gb: int) -> int:
    """Proportional toman for partial GB (floor). Used for usage debit."""
    if bytes_delta <= 0 or price_per_gb <= 0:
        return 0
    return int(int(bytes_delta) * int(price_per_gb) // GB)


# ---------------------------------------------------------------------------
# Mode / policy helpers
# ---------------------------------------------------------------------------


def is_payg(profile: ResellerProfile | None) -> bool:
    if profile is None:
        return False
    return str(getattr(profile, "billing_mode", "") or "").strip().lower() == BILLING_MODE_PAYG


async def is_billing_enabled(session: AsyncSession) -> bool:
    from app.services.users import get_setting, on

    return on(await get_setting(session, SETTING_ENABLED, "0"))


async def ensure_billing_enabled(session: AsyncSession) -> bool:
    """Turn PAYG billing on when a PAYG reseller exists. Returns True if it was switched on."""
    from app.services.users import set_setting

    if await is_billing_enabled(session):
        return False
    await set_setting(session, SETTING_ENABLED, "1")
    logger.info("PAYG billing enabled automatically for a new PAYG reseller")
    return True


async def get_on_empty_policy(session: AsyncSession) -> str:
    from app.services.users import get_setting

    raw = (await get_setting(session, SETTING_ON_EMPTY, ON_EMPTY_BLOCK_PROVISION) or "").strip()
    return raw or ON_EMPTY_BLOCK_PROVISION


async def get_low_balance_threshold(session: AsyncSession) -> int:
    from app.services.users import get_setting

    return max(0, _as_int(await get_setting(session, SETTING_LOW_BALANCE, "0"), 0))


async def get_tick_minutes(session: AsyncSession) -> int:
    from app.services.users import get_setting

    mins = _as_int(await get_setting(session, SETTING_TICK_MINUTES, "15"), 15)
    return max(1, min(1440, mins))


def payg_purchase_min_wallet(threshold: int) -> int:
    """Minimum shop-wallet balance required to buy a PAYG reseller plan.

    Must be *strictly greater* than ``2 * billing_low_balance``.
    """
    thr = max(0, int(threshold or 0))
    return (2 * thr) + 1


@dataclass(frozen=True)
class PaygWalletGate:
    """Result of the PAYG purchase wallet check."""

    ok: bool
    wallet: int
    required: int
    shortfall: int
    threshold: int

    @property
    def message(self) -> str:
        if self.ok:
            return ""
        from app.services.formatting import format_toman

        need = format_toman(self.required)
        have = format_toman(self.wallet)
        more = format_toman(self.shortfall)
        return (
            "برای خرید سرویس PAYG، موجودی کیف پول باید بیشتر از "
            f"دو برابر آستانه هشدار PAYG باشد.\n"
            f"حداقل لازم: <b>{need}</b>\n"
            f"موجودی فعلی: <b>{have}</b>\n"
            f"لطفاً حداقل <b>{more}</b> دیگر شارژ کنید."
        )

    @property
    def alert_message(self) -> str:
        """Short plain text for Telegram alert popups (no HTML)."""
        if self.ok:
            return ""
        from app.services.formatting import format_toman

        return (
            "موجودی کیف پول برای PAYG کافی نیست.\n"
            f"حداقل: {format_toman(self.required)}\n"
            f"موجودی: {format_toman(self.wallet)}\n"
            f"کمبود: {format_toman(self.shortfall)}"
        )


async def check_payg_purchase_wallet(
    session: AsyncSession,
    wallet_balance: int,
) -> PaygWalletGate:
    """Require shop wallet > 2× billing warning threshold before PAYG purchase."""
    threshold = await get_low_balance_threshold(session)
    required = payg_purchase_min_wallet(threshold)
    wallet = max(0, int(wallet_balance or 0))
    shortfall = max(0, required - wallet)
    return PaygWalletGate(
        ok=wallet >= required,
        wallet=wallet,
        required=required,
        shortfall=shortfall,
        threshold=threshold,
    )


async def assert_payg_purchase_wallet(
    session: AsyncSession,
    wallet_balance: int,
) -> PaygWalletGate:
    """Raise ``BillingError`` when wallet is below the PAYG purchase minimum."""
    gate = await check_payg_purchase_wallet(session, wallet_balance)
    if not gate.ok:
        raise BillingError(gate.alert_message)
    return gate


async def load_payg_profile(
    session: AsyncSession, reseller_user_id: int
) -> ResellerProfile | None:
    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_user_id))
        )
    ).scalar_one_or_none()
    if profile is None or not is_payg(profile):
        return None
    return profile


async def ensure_payg_shop_wallet(
    session: AsyncSession,
    profile: ResellerProfile,
) -> tuple[BotUser, int]:
    """One-time merge of legacy billing_balance into shop wallet; then wallet is SoT.

    Returns ``(BotUser, available_balance)``. ``billing_balance`` is kept as a mirror.
    """
    user = await session.get(BotUser, int(profile.user_id))
    if user is None:
        raise ValueError("کاربر نماینده یافت نشد")

    linked = bool(getattr(profile, "payg_wallet_linked", False))
    wallet = int(user.wallet_balance or 0)
    billing = int(profile.billing_balance or 0)

    if not linked:
        # Claim one-time link so concurrent first-link callers cannot re-merge
        # a stale wallet+billing snapshot and overwrite live spends/credits.
        with session.no_autoflush:
            claim = await session.execute(
                update(ResellerProfile)
                .where(
                    ResellerProfile.user_id == int(profile.user_id),
                    ResellerProfile.payg_wallet_linked.is_(False),
                )
                .values(payg_wallet_linked=True)
                .execution_options(synchronize_session=False)
            )
        if claim.rowcount != 1:
            await session.refresh(profile)
            await session.refresh(user)
            wallet = int(user.wallet_balance or 0)
            if int(profile.billing_balance or 0) != wallet:
                profile.billing_balance = wallet
            return user, wallet

        # Winner: add legacy billing into the current wallet (handles races on wallet).
        if billing != 0:
            with session.no_autoflush:
                await session.execute(
                    update(BotUser)
                    .where(BotUser.id == int(user.id))
                    .values(wallet_balance=BotUser.wallet_balance + int(billing))
                    .execution_options(synchronize_session=False)
                )
        await session.refresh(user)
        combined = int(user.wallet_balance or 0)
        profile.payg_wallet_linked = True
        profile.billing_balance = int(combined)
        logger.info(
            "PAYG linked to shop wallet reseller=%s wallet=%s billing=%s combined=%s",
            profile.user_id,
            wallet,
            billing,
            combined,
        )
        return user, int(combined)

    # Already linked — mirror only (wallet is source of truth)
    if billing != wallet:
        profile.billing_balance = wallet
    return user, wallet


async def payg_available_balance(session: AsyncSession, profile: ResellerProfile) -> int:
    """Shop-wallet balance used for PAYG (after ensuring link)."""
    _, bal = await ensure_payg_shop_wallet(session, profile)
    return int(bal)


# ---------------------------------------------------------------------------
# Ledger mutations
# ---------------------------------------------------------------------------


async def _find_by_idempotency(
    session: AsyncSession, key: str
) -> ResellerBillingTransaction | None:
    return (
        await session.execute(
            select(ResellerBillingTransaction).where(
                ResellerBillingTransaction.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


async def credit_topup(
    session: AsyncSession,
    reseller_user_id: int,
    amount: int,
    *,
    created_by: str | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> ResellerBillingTransaction:
    """Credit shop wallet for PAYG (admin path). ``billing_balance`` mirrors wallet."""
    if amount <= 0:
        raise ValueError("مبلغ شارژ باید مثبت باشد")
    key = idempotency_key
    if not key:
        raise ValueError(
            "کلید یکتای شارژ (idempotency) الزامی است — از شارژ مجدد با همان فرم خودداری کنید"
        )
    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_user_id))
        )
    ).scalar_one_or_none()
    if profile is None:
        raise ValueError("نماینده یافت نشد")
    if not is_payg(profile):
        raise ValueError("شارژ Billing فقط برای حالت PAYG است")

    existing = await _find_by_idempotency(session, key)
    if existing:
        return existing

    from app.services.billing_suspend import assert_topup_clears_suspend

    try:
        await assert_topup_clears_suspend(session, profile, int(amount))
    except BillingError as e:
        raise ValueError(e.message) from e

    user, _bal = await ensure_payg_shop_wallet(session, profile)
    uid = int(user.id)

    with session.no_autoflush:
        result = await session.execute(
            update(BotUser)
            .where(BotUser.id == uid)
            .values(wallet_balance=BotUser.wallet_balance + int(amount))
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise ValueError("نماینده یافت نشد")
    await session.refresh(user)
    profile.billing_balance = int(user.wallet_balance or 0)

    # Clear low-balance warn so next dip can notify again
    if profile.billing_low_warned_at is not None:
        profile.billing_low_warned_at = None

    tx = ResellerBillingTransaction(
        reseller_user_id=int(reseller_user_id),
        kind=KIND_TOPUP,
        amount=int(amount),
        balance_after=int(user.wallet_balance or 0),
        idempotency_key=key,
        note=(note or "")[:255] or None,
        created_by=(created_by or "")[:128] or None,
    )
    session.add(tx)
    try:
        if commit:
            await session.commit()
            await session.refresh(tx)
            await session.refresh(profile)
            await session.refresh(user)
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _find_by_idempotency(session, key)
        if existing:
            return existing
        raise

    # Instant restore when this credit clears the 2× threshold rule
    if profile.billing_suspended_at is not None:
        try:
            from app.services.billing_suspend import maybe_restore_after_wallet_credit

            await maybe_restore_after_wallet_credit(
                session, int(reseller_user_id), int(amount), commit=commit
            )
        except Exception:
            logger.exception("PAYG restore after topup failed reseller=%s", reseller_user_id)

    return tx


async def debit_adjustment(
    session: AsyncSession,
    reseller_user_id: int,
    amount: int,
    *,
    created_by: str | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> ResellerBillingTransaction:
    """Manual admin decrease of a PAYG shop wallet; the balance never goes below zero."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("مبلغ کسر باید مثبت باشد")
    if not idempotency_key:
        raise ValueError("کلید یکتای کسر (idempotency) الزامی است — صفحه را تازه کنید")
    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_user_id))
        )
    ).scalar_one_or_none()
    if profile is None:
        raise ValueError("نماینده یافت نشد")
    if not is_payg(profile):
        raise ValueError("کسر از کیف پول Billing فقط برای حالت PAYG است")

    existing = await _find_by_idempotency(session, idempotency_key)
    if existing:
        return existing

    user, _bal = await ensure_payg_shop_wallet(session, profile)
    with session.no_autoflush:
        # Atomic shortfall check: a concurrent usage charge cannot push the wallet negative.
        result = await session.execute(
            update(BotUser)
            .where(BotUser.id == int(user.id), BotUser.wallet_balance >= int(amount))
            .values(wallet_balance=BotUser.wallet_balance - int(amount))
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        await session.rollback()
        raise ValueError("موجودی کیف پول نماینده برای این کسر کافی نیست")
    await session.refresh(user)
    profile.billing_balance = int(user.wallet_balance or 0)

    tx = ResellerBillingTransaction(
        reseller_user_id=int(reseller_user_id),
        kind=KIND_ADJUSTMENT,
        amount=-int(amount),
        balance_after=int(user.wallet_balance or 0),
        idempotency_key=idempotency_key,
        note=(note or "")[:255] or None,
        created_by=(created_by or "")[:128] or None,
    )
    session.add(tx)
    try:
        if commit:
            await session.commit()
            await session.refresh(tx)
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _find_by_idempotency(session, idempotency_key)
        if existing:
            return existing
        raise
    return tx


def _watermark_covered_by_charge(
    watermark_before: int,
    watermark_after: int,
    charge: int,
    amount: int,
    rate_per_gb: int,
) -> int:
    """Watermark that matches what the wallet could actually pay.

    A full charge advances to ``watermark_after``. A partial charge advances only
    over the bytes it paid for, so the unpaid remainder is billed after a top-up.
    """
    if amount <= 0 or charge >= amount or rate_per_gb <= 0:
        return watermark_after
    paid_bytes = -(-int(charge) * GB // int(rate_per_gb))
    return min(watermark_after, watermark_before + paid_bytes)


async def _rewind_watermark(
    session: AsyncSession, reseller_user_id: int, *, claimed: int, previous: int
) -> None:
    """Move a watermark claimed by this unit of work back to ``previous``."""
    await session.execute(
        update(ResellerProfile)
        .where(
            ResellerProfile.user_id == int(reseller_user_id),
            ResellerProfile.billing_watermark_bytes == int(claimed),
        )
        .values(billing_watermark_bytes=int(previous))
        .execution_options(synchronize_session=False)
    )


async def debit_usage(
    session: AsyncSession,
    profile: ResellerProfile,
    bytes_delta: int,
    *,
    rate_per_gb: int,
    watermark_after: int,
    idempotency_key: str,
    note: str | None = None,
    commit: bool = True,
    watermark_before: int | None = None,
) -> ResellerBillingTransaction | None:
    """Charge proportional usage from shop wallet and advance watermark. Idempotent by key.

    Precision rules:
    - Charge only the *delta* since the last watermark (never re-bill prior usage).
    - If rate > 0 but the delta floors to 0 toman, do **not** advance the watermark
      so residual bytes accumulate until they are worth at least 1 toman.
    - If rate is 0, advance watermark without charging (misconfigured plan).
    - Optimistic lock on ``billing_watermark_bytes`` prevents concurrent double-charge.
    - Money is taken from shop ``wallet_balance`` (never drives balance below 0). When the
      wallet cannot pay the whole delta, the watermark advances only over the paid bytes and
      the remainder stays unbilled until the wallet is topped up.
    """
    if bytes_delta <= 0:
        profile.billing_watermark_bytes = int(watermark_after)
        if commit:
            await session.commit()
        return None

    existing = await _find_by_idempotency(session, idempotency_key)
    if existing:
        return existing

    amount = bytes_cost_proportional(bytes_delta, rate_per_gb)
    rid = int(profile.user_id)
    expected_wm = (
        int(watermark_before)
        if watermark_before is not None
        else int(profile.billing_watermark_bytes or 0)
    )

    # Hold watermark when usage is too small to bill 1 toman at a positive rate.
    if amount <= 0 and int(rate_per_gb) > 0:
        logger.debug(
            "billing hold residual reseller=%s delta=%s rate=%s",
            rid,
            bytes_delta,
            rate_per_gb,
        )
        return None

    user, wallet_before = await ensure_payg_shop_wallet(session, profile)
    uid = int(user.id)
    # Never go negative — charge only what the shop wallet holds.
    charge = min(int(amount), max(0, int(wallet_before))) if amount > 0 else 0
    if amount > 0 and charge <= 0:
        logger.warning(
            "billing held reseller=%s owed=%s — wallet empty, usage stays unbilled",
            rid,
            amount,
        )
        return None
    billed_after = _watermark_covered_by_charge(
        expected_wm, int(watermark_after), charge, int(amount), int(rate_per_gb)
    )

    if amount > 0:
        with session.no_autoflush:
            # Lock watermark first (anti double-charge)
            wm_result = await session.execute(
                update(ResellerProfile)
                .where(
                    ResellerProfile.user_id == rid,
                    ResellerProfile.billing_watermark_bytes == expected_wm,
                )
                .values(billing_watermark_bytes=billed_after)
                .execution_options(synchronize_session=False)
            )
        if wm_result.rowcount != 1:
            existing = await _find_by_idempotency(session, idempotency_key)
            if existing:
                return existing
            logger.warning(
                "billing optimistic-lock miss reseller=%s expected_wm=%s after=%s",
                rid,
                expected_wm,
                watermark_after,
            )
            await session.refresh(profile)
            return None

        if charge > 0:
            with session.no_autoflush:
                w_result = await session.execute(
                    update(BotUser)
                    .where(BotUser.id == uid, BotUser.wallet_balance >= charge)
                    .values(wallet_balance=BotUser.wallet_balance - int(charge))
                    .execution_options(synchronize_session=False)
                )
            if w_result.rowcount != 1:
                # Race: charge whatever remains, still guarded (never go negative).
                await session.refresh(user)
                remain = max(0, int(user.wallet_balance or 0))
                charge = min(charge, remain)
                if charge > 0:
                    with session.no_autoflush:
                        retry = await session.execute(
                            update(BotUser)
                            .where(
                                BotUser.id == uid,
                                BotUser.wallet_balance >= int(charge),
                            )
                            .values(
                                wallet_balance=BotUser.wallet_balance - int(charge)
                            )
                            .execution_options(synchronize_session=False)
                        )
                    if retry.rowcount != 1:
                        # Concurrent spender won again — floor at 0.
                        charge = 0
                        await session.refresh(user)
        if charge <= 0:
            await _rewind_watermark(session, rid, claimed=billed_after, previous=expected_wm)
            if commit:
                await session.commit()
            await session.refresh(profile)
            return None
        covered_after = _watermark_covered_by_charge(
            expected_wm, int(watermark_after), charge, int(amount), int(rate_per_gb)
        )
        if covered_after != billed_after:
            await _rewind_watermark(session, rid, claimed=billed_after, previous=covered_after)
            billed_after = covered_after
        await session.refresh(user)
        profile.billing_balance = int(user.wallet_balance or 0)
    else:
        # rate_per_gb == 0 — advance watermark only if still at expected
        with session.no_autoflush:
            result = await session.execute(
                update(ResellerProfile)
                .where(
                    ResellerProfile.user_id == rid,
                    ResellerProfile.billing_watermark_bytes == expected_wm,
                )
                .values(billing_watermark_bytes=int(watermark_after))
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            await session.refresh(profile)
            return None
        await session.refresh(user)
        profile.billing_balance = int(user.wallet_balance or 0)

    await session.refresh(profile)

    tx = ResellerBillingTransaction(
        reseller_user_id=rid,
        kind=KIND_USAGE,
        amount=-int(charge),
        balance_after=int(user.wallet_balance or 0),
        bytes_delta=int(bytes_delta),
        rate_per_gb=int(rate_per_gb),
        watermark_after=int(billed_after),
        idempotency_key=idempotency_key,
        note=(note or "")[:255] or None,
        created_by="billing_tick",
    )
    session.add(tx)
    try:
        if commit:
            await session.commit()
            await session.refresh(tx)
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _find_by_idempotency(session, idempotency_key)
        if existing:
            return existing
        raise
    return tx


async def list_billing_transactions(
    session: AsyncSession, reseller_user_id: int, limit: int = 20
) -> list[ResellerBillingTransaction]:
    result = await session.execute(
        select(ResellerBillingTransaction)
        .where(ResellerBillingTransaction.reseller_user_id == int(reseller_user_id))
        .order_by(ResellerBillingTransaction.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Provision gate (balance policy)
# ---------------------------------------------------------------------------


async def assert_billing_allows_provision(
    session: AsyncSession,
    *,
    reseller_user_id: int | None,
) -> None:
    """Block financially-committing ops when PAYG balance is empty.

    No-op for Fixed mode, disabled billing, or missing reseller id.
    """
    if not reseller_user_id:
        return
    if not await is_billing_enabled(session):
        return
    profile = await load_payg_profile(session, int(reseller_user_id))
    if profile is None:
        return
    policy = await get_on_empty_policy(session)
    if policy != ON_EMPTY_BLOCK_PROVISION:
        # Future policies (e.g. block_new) hook here; default remains block_provision
        return
    bal = await payg_available_balance(session, profile)
    if bal <= 0:
        # Ensure hard suspend (admin + services) even if tick missed it
        try:
            from app.services.billing_suspend import suspend_payg_reseller

            await suspend_payg_reseller(session, profile, commit=True)
        except Exception:
            logger.debug("suspend from provision gate failed", exc_info=True)
        raise BillingError(
            "موجودی کیف پول نماینده تمام شده است — حساب مسدود شد. "
            "برای رفع مسدودی کیف پول را حداقل به اندازه دو برابر آستانه هشدار شارژ کنید"
        )


# ---------------------------------------------------------------------------
# Tick: charge delta since watermark
# ---------------------------------------------------------------------------


async def tick_reseller_usage(
    session: AsyncSession,
    profile: ResellerProfile,
    admin_payload: dict[str, Any],
    *,
    traffic_source: TrafficSource | None = None,
    rate_ctx: RateContext | None = None,
    commit: bool = True,
) -> ResellerBillingTransaction | None:
    """Bill one PAYG reseller from PG admin traffic since watermark.

    Only the delta since the last billed watermark is charged, e.g. 1.0 GB then
    1.2 GB → second tick bills 0.2 GB at the plan rate.
    """
    if not is_payg(profile):
        return None
    source = traffic_source or get_traffic_source()
    used = source.extract_bytes(admin_payload)
    if used is None:
        logger.warning(
            "billing skip reseller=%s — no traffic field on admin payload",
            profile.user_id,
        )
        await _report_tick_issue_safely(session, profile, TickIssue.NO_TRAFFIC_FIELD)
        return None
    await clear_tick_issue(session, profile, PANEL_TICK_ISSUES)

    watermark = int(profile.billing_watermark_bytes or 0)
    if used < watermark:
        # Counter reset / admin recreate — reset watermark, no charge
        profile.billing_watermark_bytes = used
        if commit:
            await session.commit()
        logger.info(
            "billing watermark reset reseller=%s used=%s old_wm=%s",
            profile.user_id,
            used,
            watermark,
        )
        return None

    delta = used - watermark
    if delta <= 0:
        return None

    ctx = rate_ctx or rate_context_for_profile(profile)
    rate = await resolve_price_per_gb(session, ctx)
    if rate <= 0:
        # Hold the watermark: usage stays unbilled and is charged once a rate exists.
        logger.warning(
            "billing rate=0 reseller=%s plan_id=%s — usage held, not charged; fix PAYG plan price",
            profile.user_id,
            getattr(profile, "plan_id", None),
        )
        await _report_tick_issue_safely(session, profile, TickIssue.RATE_ZERO)
        return None
    await clear_tick_issue(session, profile, (TickIssue.RATE_ZERO,))

    key = f"usage:{int(profile.user_id)}:{watermark}:{used}"
    return await debit_usage(
        session,
        profile,
        delta,
        rate_per_gb=rate,
        watermark_after=used,
        watermark_before=watermark,
        idempotency_key=key,
        note="مصرف ترافیک",
        commit=commit,
    )


async def maybe_warn_low_balance(
    session: AsyncSession,
    profile: ResellerProfile,
) -> tuple[int, int] | None:
    """Return ``(level, balance)`` once per warning level the balance falls to."""
    if not is_payg(profile):
        return None
    balance = await payg_available_balance(session, profile)
    level = await claim_low_balance_warning(session, profile, balance)
    return None if level is None else (level, balance)


async def _report_tick_issue_safely(
    session: AsyncSession, profile: ResellerProfile, issue: TickIssue
) -> None:
    """Announce a skipped reseller without letting a send failure stop the batch."""
    try:
        await report_tick_issue(session, profile, issue)
    except Exception:
        logger.warning(
            "billing issue notify failed reseller=%s issue=%s",
            profile.user_id,
            issue.value,
            exc_info=True,
        )


async def _report_panel_outage(
    session: AsyncSession,
    unreachable: list[ResellerProfile],
    *,
    panel_answered: bool,
) -> None:
    """Report per-reseller panel failures only when the panel is otherwise reachable.

    A tick where no admin lookup succeeded is a global outage; alerting every
    reseller would only be noise.
    """
    if not unreachable:
        return
    if not panel_answered:
        logger.warning("billing tick: panel unreachable for %s resellers", len(unreachable))
        return
    for profile in unreachable:
        await _report_tick_issue_safely(session, profile, TickIssue.PANEL_UNAVAILABLE)


async def run_billing_tick(session: AsyncSession) -> dict[str, int]:
    """Charge all active PAYG resellers. Returns simple counters."""
    stats = {"checked": 0, "charged": 0, "skipped": 0, "errors": 0, "linked": 0, "restored": 0}
    if not await is_billing_enabled(session):
        return stats

    profiles = (
        await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.is_active.is_(True),
                ResellerProfile.billing_mode == BILLING_MODE_PAYG,
            )
        )
    ).scalars().all()

    if not profiles:
        return stats

    from app.services.pasarguard import get_pg

    pg = get_pg()
    panel_unreachable: list[ResellerProfile] = []
    panel_answered = False
    for profile in profiles:
        stats["checked"] += 1
        uname = (profile.pg_admin_username or "").strip()
        if not uname:
            stats["skipped"] += 1
            await _report_tick_issue_safely(session, profile, TickIssue.NO_ADMIN)
            continue
        try:
            # Link legacy billing pot → shop wallet before charging / suspend checks
            was_linked = bool(getattr(profile, "payg_wallet_linked", False))
            _user, bal = await ensure_payg_shop_wallet(session, profile)
            if not was_linked:
                stats["linked"] += 1
                await session.commit()
                # If merge left positive balance, lift a wrongful empty-billing suspend
                if bal > 0 and profile.billing_suspended_at is not None:
                    try:
                        from app.services.billing_suspend import restore_payg_reseller

                        await restore_payg_reseller(session, profile, commit=True)
                        stats["restored"] += 1
                    except Exception:
                        logger.exception(
                            "PAYG auto-restore after wallet link failed reseller=%s",
                            profile.user_id,
                        )

            gate, admin = await pg.get_admin_gate(uname)
            if gate == "missing":
                panel_answered = True
            if not isinstance(admin, dict):
                stats["skipped"] += 1
                if gate == "missing":
                    await _report_tick_issue_safely(session, profile, TickIssue.ADMIN_MISSING)
                else:
                    panel_unreachable.append(profile)
                continue
            panel_answered = True
            tx = await tick_reseller_usage(session, profile, admin, commit=True)
            if tx is not None and int(tx.amount or 0) != 0:
                stats["charged"] += 1
            else:
                stats["skipped"] += 1
            # Immediate suspend when wallet hits zero after tick
            await session.refresh(profile)
            bal_after = await payg_available_balance(session, profile)
            if bal_after <= 0:
                try:
                    from app.services.billing_suspend import suspend_payg_reseller

                    await suspend_payg_reseller(session, profile, commit=True)
                except Exception:
                    logger.exception("PAYG suspend failed reseller=%s", profile.user_id)
            else:
                warning = await maybe_warn_low_balance(session, profile)
                if warning is not None:
                    try:
                        await notify_low_balance(session, profile, *warning)
                    except Exception:
                        logger.warning(
                            "low balance notify failed reseller=%s", profile.user_id, exc_info=True
                        )
        except Exception:
            stats["errors"] += 1
            logger.exception("billing tick failed reseller=%s", profile.user_id)
    await _report_panel_outage(session, panel_unreachable, panel_answered=panel_answered)
    return stats
