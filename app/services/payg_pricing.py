"""PAYG prices for a change to a shop's panel user, computed from the shop's package.

Pure functions only: no database, no panel. The same quote is shown to the reseller
before they confirm and recomputed when the charge is taken, so the shown price and the
charged price come from one place.

Bases: ``traffic`` charges the volume set on a user, ``user`` charges each user (once,
or per started 30 days of its period), ``user_traffic`` charges both. With charging at
creation off, traffic is billed from usage instead (``billing.tick_reseller_usage``) and
these quotes carry no traffic part.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.py_compat import StrEnum

GB = 1024**3
DAYS_PER_USER_UNIT = 30
# Prices and volumes are stored in 32-bit integer columns.
MAX_PRICE = 2_000_000_000
MAX_QUANTITY = 10_000

BASIS_TRAFFIC = "traffic"
BASIS_USER = "user"
BASIS_USER_TRAFFIC = "user_traffic"
PAYG_BASES = frozenset({BASIS_TRAFFIC, BASIS_USER, BASIS_USER_TRAFFIC})

UNLIMITED_FORBIDDEN_MESSAGE = (
    "ساخت کاربر نامحدود در این پکیج مجاز نیست؛ حجم و مدت مشخص وارد کنید"
)
RESET_EXPLANATION = (
    "ریست ترافیک یعنی کل حجم این کاربر دوباره قابل مصرف می‌شود؛ "
    "به همین دلیل هزینهٔ کل حجم کسر می‌شود"
)


class ChargeEvent(StrEnum):
    CREATE = "create"
    RENEW = "renew"
    ADD_VOLUME = "add_volume"
    ADD_DAYS = "add_days"
    RESET = "reset"
    REACTIVATE = "reactivate"


# Events that start a new period for the user: full volume, full user price.
_PERIOD_EVENTS = frozenset({ChargeEvent.CREATE, ChargeEvent.RENEW, ChargeEvent.REACTIVATE})


class PaygPriceError(ValueError):
    """The change cannot be priced (forbidden unlimited user, bad input); safe to show."""


@dataclass(frozen=True, slots=True)
class PaygRates:
    basis: str
    charge_upfront: bool
    price_per_gb: int
    user_price: int
    user_price_per_30d: bool
    unlimited_price: int | None

    @property
    def charges_traffic(self) -> bool:
        return self.charge_upfront and self.basis in (BASIS_TRAFFIC, BASIS_USER_TRAFFIC)

    @property
    def charges_users(self) -> bool:
        return self.basis in (BASIS_USER, BASIS_USER_TRAFFIC)

    @property
    def bills_usage(self) -> bool:
        """Traffic is billed from consumption (the usage tick) instead of at creation."""
        return self.basis in (BASIS_TRAFFIC, BASIS_USER_TRAFFIC) and not self.charge_upfront


@dataclass(frozen=True, slots=True)
class ServiceChange:
    """A requested change. ``0``/``None`` volume or days means unlimited.

    ``days`` is the period length for create/renew/reactivate and the added days for
    ``ADD_DAYS`` (``None`` there makes the period unlimited). ``previous_limit_bytes``
    matters only for ``ADD_VOLUME``.
    """

    event: ChargeEvent
    data_limit_bytes: int | None = None
    previous_limit_bytes: int = 0
    days: int | None = None


@dataclass(frozen=True, slots=True)
class ChargeQuote:
    amount: int
    bytes_charged: int
    days_charged: int
    user_units: int
    is_unlimited: bool
    price_per_gb: int
    user_price: int

    @property
    def is_free(self) -> bool:
        return self.amount == 0

    def for_quantity(self, quantity: int) -> ChargeQuote:
        """The same change made for ``quantity`` users (wholesale); per-unit fields stay per user."""
        count = _checked_int(quantity, field="تعداد", upper=MAX_QUANTITY)
        if count < 1:
            raise PaygPriceError("تعداد نامعتبر است")
        total = self.amount * count
        if total > MAX_PRICE:
            raise PaygPriceError("مبلغ این تغییر بیش از حد مجاز است؛ تعداد را کمتر کنید")
        return replace(self, amount=total)


def _checked_int(value: Any, *, field: str, upper: int = MAX_PRICE) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > upper:
        raise PaygPriceError(f"مقدار «{field}» نامعتبر است")
    return value


def _optional_int(value: Any, *, field: str) -> int:
    return 0 if value is None else _checked_int(value, field=field, upper=2**62)


def rates_from_plan(plan: Any) -> PaygRates:
    """Rates of a reseller package; unknown basis falls back to usage-billed traffic.

    Switches count only when they are really ``True``, so a malformed value never turns
    usage billing off.
    """
    basis = str(getattr(plan, "payg_basis", "") or BASIS_TRAFFIC).strip().lower()
    unlimited = getattr(plan, "payg_unlimited_price", None)
    return PaygRates(
        basis=basis if basis in PAYG_BASES else BASIS_TRAFFIC,
        charge_upfront=getattr(plan, "payg_charge_upfront", False) is True,
        price_per_gb=_checked_int(int(getattr(plan, "price_per_gb", 0) or 0), field="نرخ هر گیگ"),
        user_price=_checked_int(int(getattr(plan, "payg_user_price", 0) or 0), field="قیمت هر کاربر"),
        user_price_per_30d=getattr(plan, "payg_user_price_per_30d", False) is True,
        unlimited_price=None if unlimited is None else _checked_int(int(unlimited), field="قیمت کاربر نامحدود"),
    )


def assert_billable_rates(rates: PaygRates) -> None:
    """Refuse a PAYG package that would bill nothing for what it sells."""
    if rates.basis in (BASIS_TRAFFIC, BASIS_USER_TRAFFIC) and rates.price_per_gb <= 0:
        raise PaygPriceError("نرخ هر گیگ این پکیج PAYG باید بیشتر از صفر باشد")
    if rates.charges_users and rates.user_price <= 0:
        raise PaygPriceError("قیمت هر کاربر این پکیج PAYG باید بیشتر از صفر باشد")


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _traffic_bytes(change: ServiceChange) -> tuple[int, bool]:
    """Bytes the change makes usable, and whether that volume is unlimited."""
    limit = _optional_int(change.data_limit_bytes, field="حجم")
    if change.event == ChargeEvent.ADD_DAYS:
        return 0, False
    if change.event == ChargeEvent.ADD_VOLUME:
        previous = _optional_int(change.previous_limit_bytes, field="حجم قبلی")
        if limit == 0:
            return 0, previous > 0
        return max(0, limit - previous), False
    return limit, limit == 0


def _user_units(rates: PaygRates, change: ServiceChange) -> tuple[int, bool]:
    """User price units the change costs, and whether its period is unlimited."""
    days = _optional_int(change.days, field="مدت")
    if change.event in _PERIOD_EVENTS:
        if not rates.user_price_per_30d:
            return 1, False
        return (_ceil_div(days, DAYS_PER_USER_UNIT), False) if days else (0, True)
    if change.event == ChargeEvent.ADD_DAYS and rates.user_price_per_30d:
        if change.days is None:
            return 0, True
        return _ceil_div(days, DAYS_PER_USER_UNIT), False
    return 0, False


def quote_change(rates: PaygRates, change: ServiceChange) -> ChargeQuote:
    """Price of ``change``; raises ``PaygPriceError`` when it may not be sold."""
    bytes_charged, unlimited_volume = (0, False)
    if rates.charges_traffic:
        bytes_charged, unlimited_volume = _traffic_bytes(change)
    units, unlimited_period = (0, False)
    if rates.charges_users:
        units, unlimited_period = _user_units(rates, change)
    unlimited = unlimited_volume or unlimited_period
    if unlimited and rates.unlimited_price is None:
        raise PaygPriceError(UNLIMITED_FORBIDDEN_MESSAGE)
    traffic_cost = 0 if unlimited_volume else _ceil_div(bytes_charged * rates.price_per_gb, GB)
    user_cost = 0 if unlimited_period else units * rates.user_price
    amount = traffic_cost + user_cost + (int(rates.unlimited_price or 0) if unlimited else 0)
    if amount > MAX_PRICE:
        raise PaygPriceError("مبلغ این تغییر بیش از حد مجاز است؛ حجم یا مدت را کمتر کنید")
    return ChargeQuote(
        amount=amount,
        bytes_charged=0 if unlimited_volume else bytes_charged,
        days_charged=_optional_int(change.days, field="مدت") if units else 0,
        user_units=units,
        is_unlimited=unlimited,
        price_per_gb=rates.price_per_gb if rates.charges_traffic else 0,
        user_price=rates.user_price if rates.charges_users else 0,
    )


def assert_quote_unchanged(expected_amount: int, quote: ChargeQuote) -> None:
    """Refuse to charge when the price changed between showing it and confirming it."""
    if isinstance(expected_amount, bool) or not isinstance(expected_amount, int) or expected_amount != quote.amount:
        raise PaygPriceError("قیمت این تغییر عوض شده است؛ قیمت تازه را ببینید و دوباره تأیید کنید")


def child_plan_id_of(profile: Any) -> int | None:
    """The level-1 price list a level-2 shop is priced with; ``None`` unless it is a real id."""
    value = getattr(profile, "child_plan_id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
