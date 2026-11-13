"""Allocate stable, shop-scoped surprise reductions without reusing protected amounts."""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, or_, select, union, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import (
    ChargeCode, DiscountCode, Order, OrderStatus, Payment, PaymentStatus, Plan,
    RandomDiscount, RandomDiscountDraw, RandomDiscountReservation,
)
from app.services.authz import authz_from_staff, can_shop
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import current_shop_reseller_id

MAX_AMOUNT = 2_000_000_000
MAX_WINDOW_MINUTES = 10080
MAX_CANDIDATES = 10000
RANDOM_DISCOUNTS_PERMISSION = "random_discounts"
_CODE_PATTERN = re.compile(r"[A-Z0-9_-]{1,64}\Z")
_OPEN_STATUSES = (
    OrderStatus.PENDING.value, OrderStatus.REJECTED.value, OrderStatus.AWAITING_RECEIPT.value,
    OrderStatus.AWAITING_APPROVAL.value, OrderStatus.PAID.value, OrderStatus.DELIVERING.value,
)
_PAYABLE_STATUSES = (OrderStatus.PENDING.value, OrderStatus.REJECTED.value)


@dataclass(frozen=True, slots=True)
class RandomDiscountInput:
    title: str
    mode: str
    reduction_kind: str
    minimum: int
    maximum: int
    step: int
    window_minutes: int
    code: str | None = None
    request_key: str = ""


@dataclass(slots=True)
class RandomDiscountDashboard:
    discounts: list[RandomDiscount]
    next_before: int | None


def random_discount_scope(staff: dict) -> int | None:
    if not can_shop(authz_from_staff(staff), RANDOM_DISCOUNTS_PERMISSION):
        raise PermissionError("دسترسی مدیریت تخفیف تصادفی ندارید")
    if is_platform_admin(staff):
        return None
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def _normalized_code(code: str | None) -> str | None:
    if code is None or code == "":
        return None
    if not isinstance(code, str):
        raise ValueError("کد تخفیف نامعتبر است")
    normalized = "".join(code.split()).upper()
    if not _CODE_PATTERN.fullmatch(normalized) or normalized.startswith("LOY"):
        raise ValueError("کد باید حداکثر ۶۴ حرف انگلیسی، عدد، خط تیره یا زیرخط باشد")
    return normalized


def _integer(value: int, minimum: int, maximum: int, message: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(message)


def validate_random_discount_input(data: RandomDiscountInput) -> str | None:
    if not isinstance(data.title, str) or not 1 <= len(data.title.strip()) <= 128:
        raise ValueError("عنوان باید بین ۱ تا ۱۲۸ نویسه باشد")
    if data.mode not in {"automatic", "code"} or data.reduction_kind not in {"amount", "percent"}:
        raise ValueError("نوع تخفیف نامعتبر است")
    ceiling = 100 if data.reduction_kind == "percent" else MAX_AMOUNT
    _integer(data.minimum, 1, ceiling, "حداقل تخفیف نامعتبر است")
    _integer(data.maximum, data.minimum, ceiling, "حداکثر تخفیف نامعتبر است")
    if data.reduction_kind == "percent" and data.minimum == 100:
        raise ValueError("حداقل درصد باید کمتر از ۱۰۰ باشد تا مبلغ پرداخت مثبت بماند")
    _integer(data.step, 1, MAX_AMOUNT, "گام مبلغ باید عدد مثبت باشد")
    _integer(data.window_minutes, 1, MAX_WINDOW_MINUTES, "بازهٔ محافظت باید بین ۱ و ۱۰۰۸۰ دقیقه باشد")
    if data.reduction_kind == "amount" and (data.maximum - data.minimum) // data.step + 1 > MAX_CANDIDATES:
        raise ValueError("تعداد مبالغ بازه بیش از ۱۰۰۰۰ است؛ گام مبلغ را افزایش دهید")
    if not isinstance(data.request_key, str) or not re.fullmatch(r"[0-9a-f]{32}", data.request_key):
        raise ValueError("شناسهٔ درخواست نامعتبر است")
    code = _normalized_code(data.code)
    if (data.mode == "code") != (code is not None):
        raise ValueError("کد فقط برای حالت استفاده با کد الزامی است")
    return code


def _policy_scope(shop_id: int | None) -> tuple[ColumnElement[bool], ...]:
    return RandomDiscount.shop_key == (shop_id or 0), RandomDiscount.reseller_id == shop_id


async def _existing_request(session: AsyncSession, shop_id: int | None, request_key: str) -> RandomDiscount | None:
    return await session.scalar(select(RandomDiscount).where(
        *_policy_scope(shop_id), RandomDiscount.request_key == request_key,
    ))


async def _assert_code_available(session: AsyncSession, code: str | None, shop_id: int | None) -> None:
    if code is None:
        return
    collision = await session.scalar(select(or_(
        exists(select(ChargeCode.id).where(ChargeCode.code == code)),
        exists(select(DiscountCode.id).where(DiscountCode.code == code)),
        exists(select(RandomDiscount.id).where(*_policy_scope(shop_id), RandomDiscount.code == code)),
    )))
    if collision:
        raise ValueError("این کد قبلاً استفاده شده است")


async def create_random_discount(session: AsyncSession, staff: dict, data: RandomDiscountInput) -> RandomDiscount:
    shop_id = random_discount_scope(staff)
    code = validate_random_discount_input(data)
    existing = await _existing_request(session, shop_id, data.request_key)
    if existing is not None:
        return existing
    await _assert_code_available(session, code, shop_id)
    row = RandomDiscount(
        shop_key=shop_id or 0, reseller_id=shop_id, title=data.title.strip(), mode=data.mode,
        reduction_kind=data.reduction_kind, minimum=data.minimum, maximum=data.maximum,
        step=data.step, window_minutes=data.window_minutes, code=code, request_key=data.request_key,
        active_slot=0, is_active=True,
    )
    try:
        session.add(row)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        existing = await _existing_request(session, shop_id, data.request_key)
        if existing is not None:
            return existing
        raise ValueError("ابتدا تخفیف تصادفی فعال را متوقف کنید؛ کد نیز باید یکتا باشد") from exc
    await session.refresh(row)
    return row


async def stop_random_discount(session: AsyncSession, staff: dict, discount_id: int) -> None:
    shop_id = random_discount_scope(staff)
    _integer(discount_id, 1, MAX_AMOUNT, "شناسهٔ تخفیف نامعتبر است")
    result = await session.execute(update(RandomDiscount).where(
        RandomDiscount.id == discount_id, *_policy_scope(shop_id),
    ).values(is_active=False, active_slot=None))
    if result.rowcount != 1:
        raise ValueError("تخفیف یافت نشد")
    await session.commit()


async def random_discount_dashboard(
    session: AsyncSession, staff: dict, *, before: int | None = None,
) -> RandomDiscountDashboard:
    shop_id = random_discount_scope(staff)
    query = select(RandomDiscount).where(*_policy_scope(shop_id))
    if before is not None:
        _integer(before, 1, MAX_AMOUNT, "صفحه نامعتبر است")
        query = query.where(RandomDiscount.id < before)
    rows = list((await session.scalars(query.order_by(RandomDiscount.id.desc()).limit(51))).all())
    return RandomDiscountDashboard(rows[:50], rows[49].id if len(rows) > 50 else None)


async def _policy_for_order(session: AsyncSession, order: Order, code: str | None) -> RandomDiscount | None:
    query = select(RandomDiscount).where(
        *_policy_scope(order.reseller_id), RandomDiscount.is_active.is_(True), RandomDiscount.active_slot == 0,
    )
    if code is None:
        query = query.where(RandomDiscount.mode == "automatic")
    else:
        # Ordinary and loyalty codes keep their existing validators and namespaces.
        normalized = "".join(code.split()).upper()
        query = query.where(RandomDiscount.mode == "code", RandomDiscount.code == normalized)
    return await session.scalar(query.limit(1))


async def _begin_sqlite_transaction(session: AsyncSession) -> None:
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        raw = await connection.get_raw_connection()
        if not raw.driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")


async def _existing_draw(session: AsyncSession, order: Order, code: str | None) -> bool:
    draw = await session.scalar(select(RandomDiscountDraw).where(
        RandomDiscountDraw.order_id == order.id, RandomDiscountDraw.shop_key == (order.reseller_id or 0),
        RandomDiscountDraw.reseller_id == order.reseller_id, RandomDiscountDraw.user_id == order.user_id,
    ))
    if draw is None:
        return False
    if order.reseller_id != current_shop_reseller_id():
        raise ValueError("سفارش این فروشگاه یافت نشد")
    policy = await session.scalar(select(RandomDiscount).where(
        RandomDiscount.id == draw.policy_id, *_policy_scope(order.reseller_id),
    ))
    if code is not None and (policy is None or policy.code != "".join(code.split()).upper()):
        raise ValueError("تخفیف تصادفی سفارش ثبت شده و با کد دیگری قابل ترکیب نیست")
    await session.refresh(order)
    return True


def _discount_limits(policy: RandomDiscount, amount: int) -> tuple[int, int]:
    if policy.reduction_kind == "percent":
        minimum = (amount * policy.minimum + 99) // 100
        maximum = amount * policy.maximum // 100
    else:
        minimum, maximum = policy.minimum, policy.maximum
    maximum = min(maximum, amount - 1)
    if minimum < 1 or minimum > maximum:
        raise ValueError("مبلغ سفارش برای بازهٔ تخفیف کافی نیست")
    return minimum, maximum


@dataclass(frozen=True, slots=True)
class _AmountRange:
    upper: int
    count: int
    step: int

    @property
    def lower(self) -> int:
        return self.upper - (self.count - 1) * self.step


def _amount_range(policy: RandomDiscount, base_amount: int) -> _AmountRange:
    minimum, maximum = _discount_limits(policy, base_amount)
    return _AmountRange(base_amount - minimum, (maximum - minimum) // policy.step + 1, policy.step)


async def _assert_not_rerolled(
    session: AsyncSession, order: Order, policy: RandomDiscount, now: datetime,
) -> None:
    previous = await session.scalar(select(RandomDiscountDraw.order_id).join(
        Order, Order.id == RandomDiscountDraw.order_id,
    ).where(
        RandomDiscountDraw.shop_key == (order.reseller_id or 0),
        RandomDiscountDraw.reseller_id == order.reseller_id, RandomDiscountDraw.user_id == order.user_id,
        RandomDiscountDraw.policy_id == policy.id, RandomDiscountDraw.base_amount == order.amount,
        RandomDiscountDraw.protected_until > now, RandomDiscountDraw.order_id != order.id,
        Order.reseller_id == order.reseller_id, Order.status != OrderStatus.DELIVERED.value,
    ).limit(1))
    if previous is not None:
        raise ValueError("برای این خرید قبلاً تخفیف انتخاب شده؛ سفارش قبلی را ادامه دهید یا بعداً تلاش کنید")


async def _blocked_amounts(
    session: AsyncSession, order: Order, amounts: _AmountRange, cutoff: datetime, now: datetime,
) -> set[int]:
    lower, upper = amounts.lower, amounts.upper
    orders = select(Order.amount).where(
        Order.reseller_id == order.reseller_id, Order.id != order.id,
        Order.amount.between(lower, upper),
        or_(Order.created_at >= cutoff, Order.updated_at >= cutoff, Order.status.in_(_OPEN_STATUSES)),
    )
    payments = select(Payment.amount).outerjoin(Order, Order.id == Payment.order_id).where(
        Payment.amount.between(lower, upper),
        or_(
            and_(Payment.order_id.is_not(None), Order.reseller_id == order.reseller_id),
            and_(Payment.order_id.is_(None), Payment.wallet_shop_id == order.reseller_id),
        ),
        or_(Payment.created_at >= cutoff, Payment.receipt_at >= cutoff, Payment.status == PaymentStatus.PENDING.value),
    )
    reserved = select(RandomDiscountReservation.final_amount).where(
        RandomDiscountReservation.shop_key == (order.reseller_id or 0),
        RandomDiscountReservation.final_amount.between(lower, upper),
        RandomDiscountReservation.protected_until > now,
    )
    blocked = union(orders, payments, reserved).subquery()
    # DISTINCT in UNION prevents one busy amount hiding others behind the limit.
    return set((await session.scalars(select(blocked.c[0]).where(
        (upper - blocked.c[0]) % amounts.step == 0,
    ).limit(MAX_CANDIDATES + 1))).all())


def _choose_free_amount(amounts: _AmountRange, blocked: set[int]) -> int:
    if len(blocked) > MAX_CANDIDATES:
        raise ValueError("تعداد مبالغ محافظت‌شده زیاد است؛ بازه یا گام تخفیف را تغییر دهید")
    if len(blocked) >= amounts.count:
        raise ValueError("مبلغ یکتا در بازهٔ تخفیف فعلاً در دسترس نیست؛ بعداً تلاش کنید")
    rank = secrets.randbelow(amounts.count - len(blocked))
    for excluded in sorted((amounts.upper - amount) // amounts.step for amount in blocked):
        if excluded > rank:
            break
        rank += 1
    return amounts.upper - rank * amounts.step


async def _reserve_amount(session: AsyncSession, draw: RandomDiscountDraw, now: datetime) -> None:
    result = await session.execute(update(RandomDiscountReservation).where(
        RandomDiscountReservation.shop_key == draw.shop_key,
        RandomDiscountReservation.final_amount == draw.final_amount,
        RandomDiscountReservation.protected_until <= now,
    ).values(order_id=draw.order_id, protected_until=draw.protected_until))
    if result.rowcount == 0:
        session.add(RandomDiscountReservation(
            shop_key=draw.shop_key, final_amount=draw.final_amount, order_id=draw.order_id,
            protected_until=draw.protected_until,
        ))
    await session.flush()


async def _claim_order(session: AsyncSession, order: Order) -> None:
    result = await session.execute(update(Order).where(
        Order.id == order.id, Order.user_id == order.user_id, Order.reseller_id == order.reseller_id,
        Order.random_discount_id.is_(None), Order.status.in_(_PAYABLE_STATUSES),
        or_(Order.discount_code.is_(None), Order.discount_code.startswith("wholesale:")),
        ~exists(select(Payment.id).where(Payment.order_id == Order.id)),
    ).values(amount=Order.amount).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ValueError("این سفارش قابل تخفیف نیست")
    await session.refresh(order)


async def _allocate_draw(session: AsyncSession, order: Order, policy: RandomDiscount) -> None:
    await _claim_order(session, order)
    _integer(order.amount, 1, MAX_AMOUNT, "مبلغ سفارش نامعتبر است")
    now = datetime.now(timezone.utc)
    await _assert_not_rerolled(session, order, policy, now)
    amounts = _amount_range(policy, order.amount)
    blocked = await _blocked_amounts(session, order, amounts, now - timedelta(minutes=policy.window_minutes), now)
    amount = _choose_free_amount(amounts, blocked)
    reduction = order.amount - amount
    draw = RandomDiscountDraw(
        shop_key=order.reseller_id or 0, reseller_id=order.reseller_id, policy_id=policy.id,
        order_id=order.id, user_id=order.user_id, base_amount=order.amount, discount_amount=reduction,
        final_amount=amount, protected_until=now + timedelta(minutes=policy.window_minutes),
    )
    await _reserve_amount(session, draw, now)
    session.add(draw)
    order.amount = amount
    order.discount_amount = int(order.discount_amount or 0) + reduction
    order.random_discount_amount = reduction
    order.random_discount_id = policy.id
    if policy.mode == "code":
        order.discount_code = policy.code
    await session.flush()


def _has_ordinary_discount(order: Order) -> bool:
    return bool(order.discount_code and not order.discount_code.startswith("wholesale:"))


async def apply_random_discount(session: AsyncSession, order: Order, *, code: str | None = None) -> bool:
    """Apply or reuse one draw without committing the caller's order transaction."""
    if order.id is None:
        await session.flush()
    if order.id is None:
        raise ValueError("ابتدا سفارش را ثبت کنید")
    if order.random_discount_id is not None or code is not None:
        if await _existing_draw(session, order, code):
            return True
    policy = await _policy_for_order(session, order, code)
    if policy is None:
        return False
    if order.reseller_id != current_shop_reseller_id():
        raise ValueError("سفارش این فروشگاه یافت نشد")
    if code is None and order.amount == 0:
        return False
    if _has_ordinary_discount(order):
        if code is not None:
            raise ValueError("تخفیف تصادفی با کد تخفیف دیگری قابل ترکیب نیست")
        return False
    if order.plan_id is not None:
        trial = await session.scalar(select(Plan.is_trial).where(Plan.id == order.plan_id))
        if trial:
            return False
    await _begin_sqlite_transaction(session)
    try:
        async with session.begin_nested():
            locked = await session.execute(update(RandomDiscount).where(
                RandomDiscount.id == policy.id, *_policy_scope(order.reseller_id),
                RandomDiscount.is_active.is_(True),
            ).values(is_active=RandomDiscount.is_active))
            if locked.rowcount != 1:
                raise ValueError("تخفیف دیگر فعال نیست")
            if await _existing_draw(session, order, code):
                return True
            await _allocate_draw(session, order, policy)
    except IntegrityError as exc:
        raise ValueError("ثبت هم‌زمان مبلغ؛ دوباره تلاش کنید") from exc
    return True
