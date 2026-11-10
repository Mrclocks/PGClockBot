"""Reads the PAYG pricing fields of the reseller package forms (create modal and edit page)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.db.models import ResellerPlan
from app.services.billing import require_positive_payg_rate
from app.services.numbers import parse_int
from app.services.payg_pricing import (
    BASIS_TRAFFIC,
    MAX_PRICE,
    PAYG_BASES,
    PaygPriceError,
    PaygRates,
    assert_billable_rates,
)

SHOWN_FIELD = "payg_fields_shown"


@dataclass(frozen=True, slots=True)
class PaygPlanSettings:
    basis: str
    charge_upfront: bool
    user_price: int
    user_price_per_30d: bool
    unlimited_price: int | None


def _money(form: Mapping[str, Any], key: str, *, label: str, optional: bool = False) -> int | None:
    raw = str(form.get(key) or "").strip()
    if not raw:
        return None if optional else 0
    try:
        value = parse_int(raw)
    except ValueError:
        raise ValueError(f"«{label}» باید عدد باشد") from None
    if value < 0 or value > MAX_PRICE:
        raise ValueError(f"«{label}» خارج از محدودهٔ مجاز است")
    return value


def payg_settings_from_form(form: Mapping[str, Any], *, price_per_gb: int) -> PaygPlanSettings | None:
    """The submitted PAYG settings, or ``None`` when the form had no PAYG fields.

    Raises ``ValueError`` (safe to show) for a bad number or a package that bills nothing.
    """
    if not form.get(SHOWN_FIELD):
        return None
    basis = str(form.get("payg_basis") or BASIS_TRAFFIC).strip().lower()
    if basis not in PAYG_BASES:
        raise ValueError("مبنای هزینهٔ PAYG نامعتبر است")
    settings = PaygPlanSettings(
        basis=basis,
        charge_upfront=bool(form.get("payg_charge_upfront")),
        user_price=int(_money(form, "payg_user_price", label="قیمت هر کاربر") or 0),
        user_price_per_30d=bool(form.get("payg_user_price_per_30d")),
        unlimited_price=_money(form, "payg_unlimited_price", label="قیمت کاربر نامحدود", optional=True),
    )
    try:
        assert_billable_rates(PaygRates(
            basis=settings.basis, charge_upfront=settings.charge_upfront, price_per_gb=int(price_per_gb),
            user_price=settings.user_price, user_price_per_30d=settings.user_price_per_30d,
            unlimited_price=settings.unlimited_price,
        ))
    except PaygPriceError as exc:
        raise ValueError(str(exc)) from exc
    return settings


def apply_payg_settings(plan: ResellerPlan, settings: PaygPlanSettings | None) -> None:
    if settings is None:
        return
    plan.payg_basis = settings.basis
    plan.payg_charge_upfront = settings.charge_upfront
    plan.payg_user_price = settings.user_price
    plan.payg_user_price_per_30d = settings.user_price_per_30d
    plan.payg_unlimited_price = settings.unlimited_price


def payg_rate_and_settings(form: Mapping[str, Any], *, billing_mode: str) -> tuple[int, PaygPlanSettings | None]:
    """Per-GB rate and PAYG settings of a submitted package form; raises ``ValueError`` (safe to show)."""
    if billing_mode != "payg":
        return 0, None
    raw = str(form.get("price_per_gb") or "").strip()
    rate = int(_money(form, "price_per_gb", label="نرخ هر گیگ") or 0) if raw else 0
    settings = payg_settings_from_form(form, price_per_gb=rate)
    if settings is None:
        require_positive_payg_rate(rate)
    return rate, settings
