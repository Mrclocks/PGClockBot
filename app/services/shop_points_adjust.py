"""Shop staff change a customer's loyalty points in their own shop by hand, within limits.

Only the shop's own points purse moves. The daily-cap claim, the points ledger row and the
staff action record are committed together; the request key makes a repeated submit
return the first result.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShopPointsAdjustDay
from app.services.daily_cap import claim_daily_cap
from app.services.db_safe import rollback_quiet
from app.services.loyalty import credit_points, debit_points, points_balance
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

POINTS_ADJUST_PERMISSION = "loyalty_adjust"
POINTS_MAX = 10_000
# Additions and deductions together, per shop per UTC day.
POINTS_DAILY_CAP = 100_000
DIRECTIONS = {"credit": "points_credit", "debit": "points_debit"}
SHORTFALL = "امتیاز مشتری برای این کسر کافی نیست"
FAILED = "تغییر امتیاز انجام نشد"


@dataclass(frozen=True, slots=True)
class ShopPointsRequest:
    customer_id: int
    points: int
    direction: str
    reason: str
    request_key: str


@dataclass(frozen=True, slots=True)
class ShopPointsChange:
    customer_id: int
    points: int
    direction: str
    balance_after: int
    replayed: bool


def points_adjust_shop(staff: dict) -> int:
    """The shop whose customers' points ``staff`` may change; raises ``PermissionError``."""
    return manual_adjust_shop(staff, POINTS_ADJUST_PERMISSION, denied="دسترسی تغییر دستی امتیاز ندارید")


def shop_points_form_context(staff: dict) -> dict[str, object]:
    """Template values for the loyalty page: the shop form only for staff who may use it."""
    try:
        points_adjust_shop(staff)
    except PermissionError:
        return {"can_adjust_shop_points": False}
    return {
        "can_adjust_shop_points": True,
        "shop_points_nonce": new_request_key(),
        "shop_points_max": POINTS_MAX,
        "shop_points_daily": POINTS_DAILY_CAP,
    }


def parse_points_request(
    *, raw_customer_id: object, raw_points: object, direction: object, reason: object, request_key: object
) -> ShopPointsRequest:
    """Validate the form values; raises ``ManualAdjustError``."""
    customer_id = parse_whole_number(raw_customer_id, low=1, high=2**31 - 1, label="شناسه کاربر")
    points = parse_whole_number(raw_points, low=1, high=POINTS_MAX, label="امتیاز")
    if direction not in DIRECTIONS:
        raise ManualAdjustError("نوع تغییر نامعتبر است")
    return ShopPointsRequest(
        customer_id=customer_id, points=points, direction=str(direction),
        reason=parse_reason(reason), request_key=parse_request_key(request_key),
    )


async def _replayed(session: AsyncSession, shop_id: int, request: ShopPointsRequest) -> ShopPointsChange | None:
    record = await earlier_record(
        session, request_key=request.request_key, shop_id=shop_id, customer_id=request.customer_id,
        action=DIRECTIONS[request.direction], amount=request.points,
    )
    if record is None:
        return None
    customer = await adjustable_customer(session, shop_id, request.customer_id)
    return ShopPointsChange(
        customer_id=request.customer_id, points=request.points, direction=request.direction,
        balance_after=int(await points_balance(session, customer, shop_id)), replayed=True,
    )


async def _move_points(session: AsyncSession, customer, *, shop_id: int, request: ShopPointsRequest, actor_label: str) -> None:
    common = {
        "tx_type": "adjust",
        "source": "shop_staff",
        "reference": None,
        "description": request.reason,
        "idempotency_key": f"shop_points:{request.request_key}",
        "created_by": actor_label,
        "commit": False,
        "shop_id": shop_id,
    }
    if request.direction == "credit":
        tx = await credit_points(session, customer, request.points, **common)
    else:
        tx = await debit_points(session, customer, request.points, allow_negative=False, **common)
    if tx is None:
        raise ManualAdjustError(FAILED)


async def adjust_shop_points(session: AsyncSession, *, staff: dict, request: ShopPointsRequest) -> ShopPointsChange:
    """Apply one manual points change to a customer in the staff member's own shop."""
    shop_id = points_adjust_shop(staff)
    replay = await _replayed(session, shop_id, request)
    if replay is not None:
        return replay
    customer = await adjustable_customer(session, shop_id, request.customer_id)
    actor = actor_from_staff(staff, shop_id=shop_id)
    try:
        if not await claim_daily_cap(session, ShopPointsAdjustDay, shop_id=shop_id, amount=request.points, cap=POINTS_DAILY_CAP):
            raise ManualAdjustError(f"سقف تغییر دستی امتیاز امروز ({POINTS_DAILY_CAP:,} امتیاز) پر شده است")
        await _move_points(session, customer, shop_id=shop_id, request=request, actor_label=actor.label)
        add_staff_action(
            session, actor, action=DIRECTIONS[request.direction], target_type="customer",
            target_id=int(customer.id), amount=request.points, reason=request.reason, request_key=request.request_key,
        )
        await session.commit()
    except IntegrityError:
        await rollback_quiet(session)
        replay = await _replayed(session, shop_id, request)
        if replay is None:
            raise
        return replay
    except ManualAdjustError:
        await rollback_quiet(session)
        raise
    except ValueError as exc:
        # After the checks above, the ledger refuses only a deduction larger than the balance.
        await rollback_quiet(session)
        raise ManualAdjustError(SHORTFALL if request.direction == "debit" else FAILED) from exc
    return ShopPointsChange(
        customer_id=int(customer.id), points=request.points, direction=request.direction,
        balance_after=int(await points_balance(session, customer, shop_id)), replayed=False,
    )
