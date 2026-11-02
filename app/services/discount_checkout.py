"""Discount and gift codes outside the bot chat (Mini App).

A discounted purchase follows the bot's order path: create the order, reserve the
code on it with ``apply_discount_to_order`` (discount, gift and loyalty codes), show
the final amount, then pay with the wallet or cancel. A failed code cancels the order
it was tried on, which releases the reservation. Gift codes that charge the wallet go
through ``redeem_charge_code``. Failed code attempts are limited per customer so codes
cannot be guessed through the API.

Each checkout belongs to one shop (``shop_id``, None for the platform): the order is
created in the current shop context, and later calls reach it only with that same shop,
so a code reserved in one shop can never be paid or cancelled from another.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus
from app.services.db_safe import rollback_quiet
from app.services.orders import apply_discount_to_order, cancel_order, create_order, pay_with_wallet
from app.services.ux20 import redeem_charge_code
from app.services.wallet import get_wallet_balance

CODE_MAX_LENGTH = 64
# Failed code attempts allowed per customer per window (discount and gift together).
CODE_FAILURE_LIMIT = 8
CODE_FAILURE_WINDOW_SECONDS = 15 * 60
_TRACKED_CUSTOMERS = 5000
# Unpaid discounted orders one customer may hold (each reserves a code use).
OPEN_CHECKOUT_LIMIT = 3
OPEN_CHECKOUT_WINDOW = timedelta(days=1)

_failures: OrderedDict[int, list[float]] = OrderedDict()


class CodeError(ValueError):
    """A code rule the customer must see (Persian, safe to show)."""


def _recent_failures(user_id: int, now: float) -> list[float]:
    stamps = [t for t in _failures.get(int(user_id), []) if now - t < CODE_FAILURE_WINDOW_SECONDS]
    if stamps:
        _failures[int(user_id)] = stamps
        _failures.move_to_end(int(user_id))
    else:
        _failures.pop(int(user_id), None)
    return stamps


def assert_code_attempts_left(user_id: int) -> None:
    if len(_recent_failures(user_id, time.monotonic())) >= CODE_FAILURE_LIMIT:
        raise CodeError("تعداد تلاش‌های ناموفق زیاد بود؛ چند دقیقه بعد دوباره امتحان کنید")


def record_code_failure(user_id: int) -> None:
    now = time.monotonic()
    stamps = _recent_failures(user_id, now)
    _failures[int(user_id)] = [*stamps, now]
    _failures.move_to_end(int(user_id))
    while len(_failures) > _TRACKED_CUSTOMERS:
        _failures.popitem(last=False)


def clean_code(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > CODE_MAX_LENGTH:
        raise CodeError("کد را درست وارد کنید")
    return value.strip()


@dataclass(slots=True)
class Checkout:
    order_id: int
    amount: int
    original_amount: int
    discount: int
    code: str

    def payload(self) -> dict:
        return {
            "order_id": self.order_id, "amount": self.amount, "original_amount": self.original_amount,
            "discount": self.discount, "code": self.code,
        }


def _checkout(order: Order) -> Checkout:
    discount = int(order.discount_amount or 0)
    return Checkout(
        order_id=int(order.id), amount=int(order.amount), original_amount=int(order.amount) + discount,
        discount=discount, code=str(order.discount_code or ""),
    )


def _shop_orders(shop_id: int | None):
    return Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)


async def _assert_open_checkout_room(session: AsyncSession, user_id: int, shop_id: int | None) -> None:
    open_count = await session.scalar(
        select(func.count()).select_from(Order).where(
            Order.user_id == int(user_id),
            _shop_orders(shop_id),
            Order.status == OrderStatus.PENDING.value,
            Order.discount_code.is_not(None),
            Order.created_at >= datetime.now(timezone.utc) - OPEN_CHECKOUT_WINDOW,
        )
    )
    if int(open_count or 0) >= OPEN_CHECKOUT_LIMIT:
        raise CodeError("چند خرید نیمه‌تمام با کد تخفیف دارید؛ ابتدا آن‌ها را پرداخت یا لغو کنید")


async def start_discounted_checkout(
    session: AsyncSession, *, user_id: int, shop_id: int | None, plan_id: int, expected_price: object, code: object,
) -> Checkout:
    """Create a plan order of ``shop_id`` with the code reserved on it; nothing is paid yet.

    The caller has set the shop context to ``shop_id``, so ``create_order`` sells only that
    shop's plans and records the order as that shop's.
    """
    clean = clean_code(code)
    assert_code_attempts_left(user_id)
    await _assert_open_checkout_room(session, user_id, shop_id)
    order = await create_order(session, user_id=int(user_id), plan_id=int(plan_id), expected_price=expected_price)
    order_id = int(order.id)
    try:
        order = await apply_discount_to_order(session, order, clean)
    except ValueError as exc:
        record_code_failure(user_id)
        await rollback_quiet(session)
        await cancel_order(session, await session.get(Order, order_id), note="mini app discount code rejected")
        raise CodeError(str(exc) or "کد تخفیف نامعتبر است") from exc
    return _checkout(order)


async def _own_open_order(session: AsyncSession, user_id: int, shop_id: int | None, order_id: int) -> Order:
    order = await session.get(Order, int(order_id))
    if (
        order is None
        or int(order.user_id) != int(user_id)
        or (int(order.reseller_id) if order.reseller_id else None) != shop_id
        or order.plan_id is None
        or order.service_id is not None
    ):
        raise CodeError("سفارش پیدا نشد")
    return order


async def pay_checkout(session: AsyncSession, *, user: BotUser, shop_id: int | None, order_id: int) -> Order:
    """Pay the customer's own order in this shop from this shop's purse; a repeat returns the same order."""
    order = await _own_open_order(session, int(user.id), shop_id, order_id)
    if order.status == OrderStatus.PENDING.value:
        await session.refresh(user)
        if int(order.amount) > await get_wallet_balance(session, user, shop_id=shop_id):
            raise CodeError("موجودی کیف پول کافی نیست")
    return await pay_with_wallet(session, order, user)


async def cancel_checkout(session: AsyncSession, *, user_id: int, shop_id: int | None, order_id: int) -> None:
    """Cancel the customer's own unpaid order in this shop and release its code."""
    order = await _own_open_order(session, user_id, shop_id, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise CodeError("این سفارش دیگر قابل لغو نیست")
    await cancel_order(session, order, note="mini app checkout cancelled")


async def redeem_wallet_code(session: AsyncSession, *, user: BotUser, code: object) -> tuple[int, int]:
    """Credit the current shop's purse (the platform's outside a shop) from a gift code.

    ``redeem_charge_code`` accepts only a code of the current shop context; returns
    (amount, new balance).
    """
    clean = clean_code(code)
    user_id = int(user.id)
    assert_code_attempts_left(user_id)
    try:
        row, balance = await redeem_charge_code(session, user=user, code=clean)
        amount = int(row.amount or 0)
        await session.commit()
    except ValueError as exc:
        await rollback_quiet(session)
        record_code_failure(user_id)
        raise CodeError(str(exc) or "کد نامعتبر است") from exc
    except OperationalError as exc:
        # SQLite refuses a second concurrent writer; nothing was credited, the customer may retry.
        await rollback_quiet(session)
        raise CodeError("سرور مشغول است؛ چند ثانیه بعد دوباره تلاش کنید") from exc
    return amount, int(balance)
