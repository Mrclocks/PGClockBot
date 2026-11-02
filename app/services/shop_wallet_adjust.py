"""Shop staff change a customer's shop wallet by hand, within fixed limits.

Only the shop's own purse (``ShopWallet`` for that shop) ever moves; the platform purse
and other shops' purses are out of reach. One request is one unit of work, committed once:
the daily-cap claim, the wallet change and the staff action record. The request key makes
a repeated submit return the first result instead of moving money again.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShopWalletAdjustDay
from app.services.daily_cap import claim_daily_cap
from app.services.db_safe import rollback_quiet
from app.services.shop_manual_adjust import (
    ManualAdjustError,
    adjustable_customer,
    earlier_record,
    manual_adjust_shop,
    new_request_key,
    parse_reason,
    parse_request_key,
    parse_whole_number,
)
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.wallet import credit_wallet, debit_wallet, get_wallet_balance

WALLET_ADJUST_PERMISSION = "wallet_adjust"
ADJUST_MIN = 1_000
ADJUST_MAX = 5_000_000
# Credits and debits together, per shop per UTC day.
DAILY_CAP = 20_000_000
DIRECTIONS = {"credit": "wallet_credit", "debit": "wallet_debit"}
SHORTFALL = "موجودی کیف پول مشتری برای این کاهش کافی نیست"
FAILED = "تغییر کیف پول انجام نشد"

# Kept as the public name callers and tests catch.
ShopWalletAdjustError = ManualAdjustError


@dataclass(frozen=True, slots=True)
class ShopWalletRequest:
    customer_id: int
    amount: int
    direction: str
    reason: str
    request_key: str


@dataclass(frozen=True, slots=True)
class ShopWalletChange:
    shop_id: int
    customer_id: int
    amount: int
    direction: str
    balance_after: int
    replayed: bool


def wallet_adjust_shop(staff: dict) -> int:
    """The shop whose purses ``staff`` may adjust; raises ``PermissionError``."""
    return manual_adjust_shop(staff, WALLET_ADJUST_PERMISSION, denied="دسترسی تنظیم کیف پول مشتری ندارید")


def shop_wallet_form_context(staff: dict) -> dict[str, object]:
    """Template values for the customer edit modal: the form only for staff who may use it."""
    try:
        wallet_adjust_shop(staff)
    except PermissionError:
        return {"can_adjust_shop_wallet": False}
    return {
        "can_adjust_shop_wallet": True,
        "shop_wallet_nonce": new_request_key(),
        "shop_wallet_max": ADJUST_MAX,
        "shop_wallet_daily": DAILY_CAP,
    }


def parse_request(*, customer_id: int, raw_amount: object, direction: object, reason: object, request_key: object) -> ShopWalletRequest:
    """Validate the form values; raises ``ShopWalletAdjustError``."""
    amount = parse_whole_number(raw_amount, low=ADJUST_MIN, high=ADJUST_MAX, label="مبلغ (تومان)")
    if direction not in DIRECTIONS:
        raise ShopWalletAdjustError("نوع تغییر نامعتبر است")
    return ShopWalletRequest(
        customer_id=int(customer_id), amount=amount, direction=str(direction),
        reason=parse_reason(reason), request_key=parse_request_key(request_key),
    )


async def _replayed(session: AsyncSession, shop_id: int, request: ShopWalletRequest) -> ShopWalletChange | None:
    record = await earlier_record(
        session, request_key=request.request_key, shop_id=shop_id, customer_id=request.customer_id,
        action=DIRECTIONS[request.direction], amount=request.amount,
    )
    if record is None:
        return None
    balance = await get_wallet_balance(session, request.customer_id, shop_id=shop_id)
    return ShopWalletChange(
        shop_id=shop_id, customer_id=request.customer_id, amount=request.amount,
        direction=request.direction, balance_after=int(balance), replayed=True,
    )


async def adjust_shop_wallet(session: AsyncSession, *, staff: dict, request: ShopWalletRequest) -> ShopWalletChange:
    """Apply one manual change to a customer's wallet in the staff member's own shop."""
    shop_id = wallet_adjust_shop(staff)
    replay = await _replayed(session, shop_id, request)
    if replay is not None:
        return replay
    customer = await adjustable_customer(session, shop_id, request.customer_id)
    actor = actor_from_staff(staff, shop_id=shop_id)
    reason = f"تنظیم دستی فروشگاه ({actor.label[:64]}): {request.reason}"
    try:
        if not await claim_daily_cap(session, ShopWalletAdjustDay, shop_id=shop_id, amount=request.amount, cap=DAILY_CAP):
            raise ShopWalletAdjustError(f"سقف تغییر دستی کیف پول امروز ({DAILY_CAP:,} تومان) پر شده است")
        move = credit_wallet if request.direction == "credit" else debit_wallet
        await move(session, customer, request.amount, reason, shop_id=shop_id, commit=False)
        add_staff_action(
            session, actor, action=DIRECTIONS[request.direction], target_type="customer",
            target_id=int(customer.id), amount=request.amount, reason=request.reason, request_key=request.request_key,
        )
        await session.commit()
    except IntegrityError:
        # The same request key won a concurrent race: report that first result.
        await rollback_quiet(session)
        replay = await _replayed(session, shop_id, request)
        if replay is None:
            raise
        return replay
    except ShopWalletAdjustError:
        await rollback_quiet(session)
        raise
    except ValueError as exc:
        # After the checks above, the wallet refuses only a debit larger than the balance.
        await rollback_quiet(session)
        raise ShopWalletAdjustError(SHORTFALL if request.direction == "debit" else FAILED) from exc
    balance = await get_wallet_balance(session, customer, shop_id=shop_id)
    return ShopWalletChange(
        shop_id=shop_id, customer_id=int(customer.id), amount=request.amount,
        direction=request.direction, balance_after=int(balance), replayed=False,
    )
