"""Unified reseller Billing service.

Pay As You Go is one ``billing_mode``; Fixed commission stays on
``ResellerProfile.balance`` and is never touched here.

All money movement for PAYG must go through this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerBillingRate, ResellerBillingTransaction, ResellerProfile

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
    """Resolve toman/GB. Rates table overrides win by specificity; else Setting."""
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

    if rid is not None:
        hit = _match("reseller", "", rid)
        if hit is not None:
            return hit

    hit = _match("default", "", None)
    if hit is not None:
        return hit

    from app.services.users import get_setting

    raw = await get_setting(session, SETTING_PRICE_PER_GB, "0")
    return max(0, _as_int(raw, 0))


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
    """Manual (or future online) credit — Super Admin MVP path."""
    if amount <= 0:
        raise ValueError("مبلغ شارژ باید مثبت باشد")
    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_user_id))
        )
    ).scalar_one_or_none()
    if profile is None:
        raise ValueError("نماینده یافت نشد")
    if not is_payg(profile):
        raise ValueError("شارژ Billing فقط برای حالت Pay As You Go است")

    key = idempotency_key or f"topup:{reseller_user_id}:{amount}:{datetime.now(timezone.utc).timestamp()}"
    existing = await _find_by_idempotency(session, key)
    if existing:
        return existing

    with session.no_autoflush:
        result = await session.execute(
            update(ResellerProfile)
            .where(ResellerProfile.user_id == int(reseller_user_id))
            .values(billing_balance=ResellerProfile.billing_balance + int(amount))
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise ValueError("نماینده یافت نشد")
    await session.refresh(profile)

    # Clear low-balance warn so next dip can notify again
    if profile.billing_low_warned_at is not None:
        profile.billing_low_warned_at = None

    tx = ResellerBillingTransaction(
        reseller_user_id=int(reseller_user_id),
        kind=KIND_TOPUP,
        amount=int(amount),
        balance_after=int(profile.billing_balance),
        idempotency_key=key,
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
        existing = await _find_by_idempotency(session, key)
        if existing:
            return existing
        raise
    return tx


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
) -> ResellerBillingTransaction | None:
    """Charge proportional usage and advance watermark. Idempotent by key."""
    if bytes_delta <= 0:
        # Still advance watermark when traffic moved but cost is 0
        profile.billing_watermark_bytes = int(watermark_after)
        if commit:
            await session.commit()
        return None

    existing = await _find_by_idempotency(session, idempotency_key)
    if existing:
        return existing

    amount = bytes_cost_proportional(bytes_delta, rate_per_gb)
    rid = int(profile.user_id)

    if amount > 0:
        with session.no_autoflush:
            result = await session.execute(
                update(ResellerProfile)
                .where(
                    ResellerProfile.user_id == rid,
                    ResellerProfile.billing_balance >= int(amount),
                )
                .values(
                    billing_balance=ResellerProfile.billing_balance - int(amount),
                    billing_watermark_bytes=int(watermark_after),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                # Floor: refuse debit that would drive balance negative (race-safe).
                await session.refresh(profile)
                raise BillingError(
                    "موجودی صورتحساب برای کسر مصرف کافی نیست"
                )
    else:
        profile.billing_watermark_bytes = int(watermark_after)

    await session.refresh(profile)

    tx = ResellerBillingTransaction(
        reseller_user_id=rid,
        kind=KIND_USAGE,
        amount=-int(amount),
        balance_after=int(profile.billing_balance),
        bytes_delta=int(bytes_delta),
        rate_per_gb=int(rate_per_gb),
        watermark_after=int(watermark_after),
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
    bal = int(profile.billing_balance or 0)
    if bal <= 0:
        raise BillingError(
            "موجودی Billing نماینده تمام شده است — ابتدا شارژ کنید "
            "یا با ادمین اصلی تماس بگیرید"
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
    """Bill one PAYG reseller from PG admin traffic since watermark."""
    if not is_payg(profile):
        return None
    source = traffic_source or get_traffic_source()
    used = source.extract_bytes(admin_payload)
    if used is None:
        return None

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

    rate = await resolve_price_per_gb(
        session,
        rate_ctx
        or RateContext(reseller_user_id=int(profile.user_id)),
    )
    key = f"usage:{int(profile.user_id)}:{watermark}:{used}"
    try:
        return await debit_usage(
            session,
            profile,
            delta,
            rate_per_gb=rate,
            watermark_after=used,
            idempotency_key=key,
            note="مصرف ترافیک",
            commit=commit,
        )
    except BillingError as exc:
        # Race / empty balance: do not advance watermark; retry next tick.
        logger.warning(
            "billing debit skipped reseller=%s amount_delta=%s: %s",
            profile.user_id,
            delta,
            getattr(exc, "message", exc),
        )
        return None


async def maybe_warn_low_balance(
    session: AsyncSession,
    profile: ResellerProfile,
) -> bool:
    """Return True once when balance crosses below threshold (needs notify)."""
    if not is_payg(profile):
        return False
    threshold = await get_low_balance_threshold(session)
    if threshold <= 0:
        return False
    bal = int(profile.billing_balance or 0)
    if bal > threshold:
        if profile.billing_low_warned_at is not None:
            profile.billing_low_warned_at = None
            await session.commit()
        return False
    if profile.billing_low_warned_at is not None:
        return False
    profile.billing_low_warned_at = datetime.now(timezone.utc)
    await session.commit()
    return True


async def run_billing_tick(session: AsyncSession) -> dict[str, int]:
    """Charge all active PAYG resellers. Returns simple counters."""
    stats = {"checked": 0, "charged": 0, "skipped": 0, "errors": 0}
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
    for profile in profiles:
        stats["checked"] += 1
        uname = (profile.pg_admin_username or "").strip()
        if not uname:
            stats["skipped"] += 1
            continue
        try:
            admin = await pg.get_admin(uname)
            if not isinstance(admin, dict):
                stats["skipped"] += 1
                continue
            tx = await tick_reseller_usage(session, profile, admin, commit=True)
            if tx is not None and int(tx.amount or 0) != 0:
                stats["charged"] += 1
            else:
                stats["skipped"] += 1
            if await maybe_warn_low_balance(session, profile):
                try:
                    await _notify_low_balance(session, profile)
                except Exception:
                    logger.debug("low balance notify failed", exc_info=True)
        except Exception:
            stats["errors"] += 1
            logger.exception("billing tick failed reseller=%s", profile.user_id)
    return stats


async def _notify_low_balance(session: AsyncSession, profile: ResellerProfile) -> None:
    from app.db.models import BotUser
    from app.services.formatting import format_toman

    user = await session.get(BotUser, int(profile.user_id))
    if not user or not user.telegram_id:
        return
    bal = format_toman(int(profile.billing_balance or 0))
    text = (
        "⚠️ موجودی Billing شما رو به اتمام است.\n"
        f"مانده: <b>{bal}</b>\n"
        "برای ادامه ساخت/تمدید سرویس، با ادمین اصلی برای شارژ هماهنگ کنید."
    )
    try:
        from app.bot import create_bot
        from app.services.reseller_bots import open_notify_bot_for_reseller

        shop_bot, owned = await open_notify_bot_for_reseller(session, int(profile.user_id))
        bot = shop_bot
        close = owned
        if bot is None:
            bot = create_bot()
            close = True
        try:
            await bot.send_message(int(user.telegram_id), text, parse_mode="HTML")
        finally:
            if close and bot is not None:
                await bot.session.close()
    except Exception:
        logger.debug("billing low-balance telegram failed", exc_info=True)


def should_credit_fixed_commission(profile: ResellerProfile | None) -> bool:
    """Fixed-mode only; PAYG must not touch commission balance."""
    if profile is None:
        return False
    return not is_payg(profile)
