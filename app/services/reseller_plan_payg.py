"""Owner edits of a reseller package's PasarGuard access switch and PAYG options, one at a time.

The bot changes one option per tap, so each change is validated against the whole
package before it is saved: a PAYG package must keep billing something
(``payg_pricing.assert_billable_rates``). The web form saves the same columns.
Changes apply to shops that get the package afterwards and to later charges; charges
already taken keep their amount.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerPlan
from app.py_compat import StrEnum
from app.services.numbers import parse_int
from app.services.payg_pricing import MAX_PRICE, PAYG_BASES, PaygPriceError, assert_billable_rates, rates_from_plan


class PlanFlag(StrEnum):
    PG_ACCESS = "acc"
    UPFRONT = "up"
    PER_30_DAYS = "p30"


class PlanPrice(StrEnum):
    USER = "user"
    UNLIMITED = "unl"


_FLAG_COLUMNS = {
    PlanFlag.PG_ACCESS: "allow_pg_panel_access",
    PlanFlag.UPFRONT: "payg_charge_upfront",
    PlanFlag.PER_30_DAYS: "payg_user_price_per_30d",
}
_PRICE_COLUMNS = {PlanPrice.USER: "payg_user_price", PlanPrice.UNLIMITED: "payg_unlimited_price"}


def is_payg_plan(plan: ResellerPlan) -> bool:
    return str(getattr(plan, "billing_mode", "") or "").lower() == "payg"


async def _plan(session: AsyncSession, plan_id: int) -> ResellerPlan:
    plan = await session.get(ResellerPlan, int(plan_id))
    if plan is None:
        raise ValueError("پکیج یافت نشد")
    return plan


async def _save(session: AsyncSession, plan: ResellerPlan) -> ResellerPlan:
    """Commit when the package still bills something; otherwise undo the change."""
    if is_payg_plan(plan):
        try:
            assert_billable_rates(rates_from_plan(plan))
        except PaygPriceError as exc:
            await session.rollback()
            raise ValueError(str(exc)) from exc
    await session.commit()
    return plan


def _payg_only(plan: ResellerPlan) -> None:
    if not is_payg_plan(plan):
        raise ValueError("این گزینه فقط برای پکیج PAYG است")


async def toggle_plan_flag(session: AsyncSession, plan_id: int, flag: PlanFlag) -> ResellerPlan:
    plan = await _plan(session, plan_id)
    if flag != PlanFlag.PG_ACCESS:
        _payg_only(plan)
    column = _FLAG_COLUMNS[flag]
    current = getattr(plan, column, None)
    default_on = flag == PlanFlag.PG_ACCESS
    setattr(plan, column, not (default_on if current is None else bool(current)))
    return await _save(session, plan)


async def set_plan_basis(session: AsyncSession, plan_id: int, basis: str) -> ResellerPlan:
    plan = await _plan(session, plan_id)
    _payg_only(plan)
    if basis not in PAYG_BASES:
        raise ValueError("مبنای هزینهٔ PAYG نامعتبر است")
    plan.payg_basis = basis
    return await _save(session, plan)


def parse_plan_price(text: str | None, price: PlanPrice) -> int | None:
    """A typed price; «-» forbids unlimited users. Raises ``ValueError`` (safe to show)."""
    raw = (text or "").strip()
    if price == PlanPrice.UNLIMITED and raw in {"-", "—"}:
        return None
    try:
        value = parse_int(raw)
    except ValueError:
        raise ValueError("یک عدد وارد کنید") from None
    if value < 0 or value > MAX_PRICE:
        raise ValueError("مبلغ خارج از محدودهٔ مجاز است")
    return value


async def set_plan_price(session: AsyncSession, plan_id: int, price: PlanPrice, value: int | None) -> ResellerPlan:
    plan = await _plan(session, plan_id)
    _payg_only(plan)
    if value is None and price != PlanPrice.UNLIMITED:
        raise ValueError("یک عدد وارد کنید")
    setattr(plan, _PRICE_COLUMNS[price], value)
    return await _save(session, plan)
