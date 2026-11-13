"""Gift-code rules and transactional checkout reservations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    ChargeCode,
    ChargeCodeUse,
    DiscountCode,
    Order,
    OrderStatus,
    Plan,
    RandomDiscount,
)
from app.services.authz import authz_from_staff, can_shop
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.staff_audit import StaffActor, add_staff_action

TEHRAN = ZoneInfo("Asia/Tehran")
PURCHASE_TYPES = {
    "new": "خرید جدید",
    "renew": "تمدید",
    "volume": "افزایش حجم",
    "duration": "افزایش زمان",
}
MAX_AMOUNT = 50_000_000
MAX_USES = 1000
GIFT_CODE_LIST_LIMIT = 200
# A code limited to plans names at most this many; the form lists at most PLAN_OPTION_LIMIT.
MAX_CODE_PLANS = 40
PLAN_OPTION_LIMIT = 100
PLAN_ID_MAX = 2**31 - 1
# Add-ons are not bought with a plan, so a plan limit only fits these purchases.
PLAN_PURCHASE_TYPES = frozenset({"new", "renew"})
ALL_PLANS = "همه پلن‌ها"
DELETED_PLANS = "پلن حذف‌شده"
# Gift codes mint wallet credit in the shop, so they have their own key, apart from orders or plans.
GIFT_CODES_PERMISSION = "gift_codes"


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def expiry_input(value: datetime | None) -> str:
    # Space separator matches the text field placeholder (easier to type on mobile).
    return utc(value).astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M") if value else ""


def parse_expiry(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    # Accept "YYYY-MM-DD HH:MM", "YYYY-MM-DDTHH:MM", and ISO with seconds/offset.
    normalized = text
    if "T" not in normalized and " " in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("تاریخ انقضا نامعتبر است") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=TEHRAN)
    return utc(value)


def _plan_id_list(plan_ids) -> list[int]:
    """Sorted distinct plan ids from a form list or a stored comma-separated value."""
    raw = plan_ids.split(",") if isinstance(plan_ids, str) else list(plan_ids or [])
    ids: set[int] = set()
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if isinstance(item, bool) or not text.isdigit() or not 0 < int(text) <= PLAN_ID_MAX:
            raise ValueError("پلن انتخاب‌شده نامعتبر است")
        ids.add(int(text))
    if len(ids) > MAX_CODE_PLANS:
        raise ValueError(f"حداکثر {MAX_CODE_PLANS} پلن برای یک کد قابل انتخاب است")
    return sorted(ids)


def _check_plan_limit(kind: str, types: set[str], plans: list[int]) -> None:
    if not plans:
        return
    if kind != "discount":
        raise ValueError("محدودیت پلن فقط برای کد تخفیف قابل تنظیم است")
    if types and not types & PLAN_PURCHASE_TYPES:
        raise ValueError("کد محدود به پلن فقط برای خرید جدید یا تمدید قابل استفاده است")


def code_plan_ids(row: ChargeCode) -> set[int]:
    """The plans a code is limited to; empty means every plan."""
    return {int(part) for part in (row.plan_ids or "").split(",") if part.strip().isdigit()}


def validate_rules(
    *,
    kind="wallet",
    amount=0,
    percent=0,
    max_discount_toman=None,
    max_uses=1,
    max_uses_per_user=None,
    expires_at=None,
    first_purchase_only=False,
    purchase_types=None,
    plan_ids=None,
) -> dict:
    if kind not in {"wallet", "discount"}:
        raise ValueError("نوع کد نامعتبر است")
    types = set(
        purchase_types.split(",")
        if isinstance(purchase_types, str)
        else purchase_types or []
    )
    if types - PURCHASE_TYPES.keys():
        raise ValueError("نوع خرید نامعتبر است")
    plans = _plan_id_list(plan_ids)
    _check_plan_limit(kind, types, plans)
    if kind == "wallet" and (first_purchase_only or types):
        raise ValueError("محدودیت نوع خرید فقط برای کد تخفیف قابل تنظیم است")
    if first_purchase_only and types and "new" not in types:
        raise ValueError("کد خرید اول باید برای خرید جدید قابل استفاده باشد")
    amount, percent = int(amount), int(percent)
    if kind == "wallet" and not 0 < amount <= MAX_AMOUNT:
        raise ValueError(f"مبلغ کد هدیه باید بین ۱ و {MAX_AMOUNT:,} تومان باشد")
    if kind == "discount" and not 1 <= percent <= 100:
        raise ValueError("درصد تخفیف باید بین ۱ تا ۱۰۰ باشد")
    for value in (max_uses, max_uses_per_user):
        if value is not None and not 1 <= int(value) <= MAX_USES:
            raise ValueError(f"سقف تعداد استفاده باید بین ۱ تا {MAX_USES} باشد")
    if max_discount_toman is not None and not 0 < int(max_discount_toman) <= MAX_AMOUNT:
        raise ValueError(f"سقف تخفیف باید بین ۱ و {MAX_AMOUNT:,} تومان باشد")
    return dict(
        kind=kind,
        amount=amount if kind == "wallet" else 0,
        percent=percent if kind == "discount" else 0,
        max_discount_toman=int(max_discount_toman)
        if kind == "discount" and max_discount_toman is not None
        else None,
        max_uses=int(max_uses) if max_uses is not None else None,
        max_uses_per_user=int(max_uses_per_user)
        if max_uses_per_user is not None
        else None,
        expires_at=utc(expires_at) if expires_at else None,
        first_purchase_only=bool(first_purchase_only),
        purchase_types=",".join(key for key in PURCHASE_TYPES if key in types) or None,
        plan_ids=",".join(str(plan_id) for plan_id in plans) or None,
    )


def _shop_plans(shop_id: int | None):
    return Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)


async def check_code_plans(session: AsyncSession, plan_ids: str | None, *, shop_id: int | None) -> None:
    """Refuse plans that are not this shop's own sellable plans (another shop's, or a trial)."""
    wanted = _plan_id_list(plan_ids)
    if not wanted:
        return
    found = await session.scalar(
        select(func.count(Plan.id)).where(
            Plan.id.in_(wanted), _shop_plans(shop_id), Plan.is_trial.is_(False)
        )
    )
    if int(found or 0) != len(wanted):
        raise ValueError("پلن انتخاب‌شده متعلق به این فروشگاه نیست")


async def gift_code_plan_options(session: AsyncSession, *, shop_id: int | None) -> list[tuple[int, str, bool]]:
    """``(id, name, is_active)`` of the shop's plans a discount code can be limited to; bounded."""
    rows = await session.execute(
        select(Plan.id, Plan.name, Plan.is_active)
        .where(_shop_plans(shop_id), Plan.is_trial.is_(False))
        .order_by(Plan.is_active.desc(), Plan.id)
        .limit(PLAN_OPTION_LIMIT)
    )
    return [(int(row.id), row.name, bool(row.is_active)) for row in rows]


async def _assert_gift_code_available(session: AsyncSession, code: str, *, shop_id: int | None) -> None:
    collision = await session.scalar(select(or_(
        exists(select(ChargeCode.id).where(ChargeCode.code == code)),
        exists(select(DiscountCode.id).where(DiscountCode.code == code)),
        exists(select(RandomDiscount.id).where(
            RandomDiscount.shop_key == (shop_id or 0), RandomDiscount.reseller_id == shop_id,
            RandomDiscount.code == code,
        )),
    )))
    if collision:
        raise ValueError("این کد از قبل وجود دارد")


async def create_gift_code(
    session: AsyncSession, *, code=None, note=None, reseller_id=None, audit: StaffActor | None = None, **rules
) -> ChargeCode:
    from app.services.ux20 import generate_charge_code, normalize_charge_code

    values = validate_rules(**rules)
    if values["expires_at"] and values["expires_at"] <= datetime.now(timezone.utc):
        raise ValueError("تاریخ انقضا باید در آینده باشد")
    await check_code_plans(session, values["plan_ids"], shop_id=reseller_id)
    key = normalize_charge_code(code) if code else generate_charge_code()
    if (
        not key
        or len(key) > 64
        or key.startswith("LOY")
        or key.startswith("WHOLESALE:")
    ):
        raise ValueError("کد نامعتبر است؛ حداکثر ۶۴ کاراکتر و بدون پیشوند رزروشده")
    await _assert_gift_code_available(session, key, shop_id=reseller_id)
    row = ChargeCode(
        code=key,
        note=(note or "").strip()[:255] or None,
        reseller_id=reseller_id,
        **values,
    )
    session.add(row)
    try:
        if audit is not None:
            await session.flush()
            add_staff_action(
                session, audit, action="gift_code_create", target_type="gift_code", target_id=int(row.id),
                amount=int(row.amount or 0) if row.kind == "wallet" else None,
            )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError(
            "این کد از قبل وجود دارد یا اطلاعات فروشگاه نامعتبر است"
        ) from exc
    await session.refresh(row)
    return row


def check_available(row: ChargeCode, *, shop_id: int | None, kind: str) -> None:
    if not row.is_active:
        raise ValueError("کد غیرفعال است")
    if row.reseller_id != shop_id:
        raise ValueError("این کد برای فروشگاه شما نیست")
    if row.kind != kind:
        if row.kind == "discount":
            raise ValueError(
                "این کد تخفیف خرید است؛ آن را هنگام پرداخت در بخش «کد تخفیف» وارد کنید"
            )
        raise ValueError("این کد شارژ کیف پول است؛ آن را در بخش «کد هدیه» وارد کنید")
    if row.expires_at and utc(row.expires_at) <= datetime.now(timezone.utc):
        raise ValueError("تاریخ اعتبار کد به پایان رسیده است")
    if row.max_uses is not None and row.used_count >= row.max_uses:
        raise ValueError("سقف استفاده از کد پر شده")


async def claim_use(
    session: AsyncSession,
    row: ChargeCode,
    *,
    user_id: int,
    shop_id: int | None,
    kind: str,
) -> None:
    """The code UPDATE serializes claims on both PostgreSQL and SQLite.

    Caller must wrap the claim and entitlement in one savepoint/transaction.
    Check user counts AFTER taking the code lock, so parallel requests see
    the preceding committed reservation.
    """
    claim = await session.execute(
        update(ChargeCode)
        .where(ChargeCode.id == row.id)
        .values(used_count=ChargeCode.used_count)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        raise ValueError("کد نامعتبر است")
    await session.refresh(row)
    check_available(row, shop_id=shop_id, kind=kind)
    if row.max_uses_per_user is not None:
        used = (
            await session.execute(
                select(func.count(ChargeCodeUse.id)).where(
                    ChargeCodeUse.charge_code_id == row.id,
                    ChargeCodeUse.user_id == user_id,
                    ChargeCodeUse.status.in_(("reserved", "consumed")),
                )
            )
        ).scalar_one()
        if used >= row.max_uses_per_user:
            raise ValueError("سقف استفاده شما از این کد پر شده است")
    await session.execute(
        update(ChargeCode)
        .where(ChargeCode.id == row.id)
        .values(used_count=ChargeCode.used_count + 1)
        .execution_options(synchronize_session="fetch")
    )


async def order_purchase_type(session: AsyncSession, order: Order) -> str | None:
    note = (order.note or "").strip()
    if note.startswith("renew:"):
        return "renew"
    if note.startswith("svc_addon:"):
        from app.db.models import ServiceAddonPack
        from app.services.service_addons import parse_addon_note

        parsed = parse_addon_note(note)
        if not parsed:
            return None
        if parsed[2]:
            return parsed[2]
        pack = await session.get(ServiceAddonPack, parsed[0])
        return pack.kind if pack else None
    if note.startswith("reseller_app:") or not order.plan_id:
        return None
    plan = await session.get(Plan, order.plan_id)
    return "new" if plan and not plan.is_trial else None


async def check_first_purchase(
    session: AsyncSession, row: ChargeCode, order: Order
) -> None:
    scope = (
        Order.reseller_id == order.reseller_id
        if order.reseller_id
        else Order.reseller_id.is_(None)
    )
    prior = (
        await session.execute(
            select(Order.id)
            .outerjoin(Plan, Plan.id == Order.plan_id)
            .where(
                Order.user_id == order.user_id,
                Order.id != order.id,
                scope,
                Order.status.in_(
                    (
                        OrderStatus.PAID.value,
                        OrderStatus.DELIVERING.value,
                        OrderStatus.DELIVERED.value,
                    )
                ),
                Order.amount + Order.discount_amount > 0,
                or_(Plan.is_trial.is_(False), Order.note.like("svc_addon:%")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    reserved = (
        await session.execute(
            select(ChargeCodeUse.id)
            .join(ChargeCode)
            .where(
                ChargeCodeUse.user_id == order.user_id,
                ChargeCodeUse.status == "reserved",
                ChargeCodeUse.order_id != order.id,
                ChargeCode.first_purchase_only.is_(True),
                ChargeCode.reseller_id == row.reseller_id
                if row.reseller_id
                else ChargeCode.reseller_id.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior or reserved:
        raise ValueError("این کد فقط برای اولین خرید شما در این فروشگاه است")


def compute_discount(row: ChargeCode, base: int) -> int:
    amount = max(0, int(base)) * max(0, min(100, row.percent)) // 100
    return (
        min(amount, row.max_discount_toman)
        if row.max_discount_toman is not None
        else amount
    )


async def check_purchase_rules(
    session: AsyncSession, row: ChargeCode, order: Order
) -> None:
    purchase_type = await order_purchase_type(session, order)
    allowed = (row.purchase_types or "").split(",")
    if purchase_type is None or (row.purchase_types and purchase_type not in allowed):
        raise ValueError("این کد برای نوع این خرید قابل استفاده نیست")
    plans = code_plan_ids(row)
    if plans and int(order.plan_id or 0) not in plans:
        raise ValueError("این کد برای این پلن قابل استفاده نیست")
    if row.first_purchase_only:
        if purchase_type != "new":
            raise ValueError("این کد فقط برای اولین خرید جدید است")
        await check_first_purchase(session, row, order)


async def preview_gift_discount(
    session: AsyncSession, row: ChargeCode, base: int, *, shop_id=None, order=None
) -> tuple[int, str | None]:
    try:
        check_available(
            row, shop_id=order.reseller_id if order else shop_id, kind="discount"
        )
        if order is None and (
            row.first_purchase_only
            or row.purchase_types
            or row.plan_ids
            or row.max_uses_per_user is not None
        ):
            return 0, None
        if order:
            await check_purchase_rules(session, row, order)
            if row.max_uses_per_user is not None:
                used = (
                    await session.execute(
                        select(func.count(ChargeCodeUse.id)).where(
                            ChargeCodeUse.charge_code_id == row.id,
                            ChargeCodeUse.user_id == order.user_id,
                            ChargeCodeUse.status.in_(("reserved", "consumed")),
                        )
                    )
                ).scalar_one()
                if used >= row.max_uses_per_user:
                    return 0, None
        discount = compute_discount(row, base)
        return (discount, row.code) if discount > 0 else (0, None)
    except ValueError:
        return 0, None


async def reserve_gift_discount(
    session: AsyncSession, row: ChargeCode, order: Order, base: int
) -> tuple[int, str]:
    async with session.begin_nested():
        await session.execute(
            update(BotUser)
            .where(BotUser.id == order.user_id)
            .values(wallet_balance=BotUser.wallet_balance)
            .execution_options(synchronize_session=False)
        )
        await claim_use(
            session,
            row,
            user_id=order.user_id,
            shop_id=order.reseller_id,
            kind="discount",
        )
        await check_purchase_rules(session, row, order)
        discount = compute_discount(row, base)
        if discount <= 0:
            raise ValueError("این سفارش مبلغ قابل تخفیف ندارد")
        session.add(
            ChargeCodeUse(
                charge_code_id=row.id, user_id=order.user_id, order_id=order.id
            )
        )
        await session.flush()
        return discount, row.code


async def release_gift_discount(session: AsyncSession, order: Order) -> bool:
    row = (
        await session.execute(
            select(ChargeCode).where(ChargeCode.code == order.discount_code)
        )
    ).scalar_one_or_none()
    if not row:
        return False
    released = await session.execute(
        update(ChargeCodeUse)
        .where(
            ChargeCodeUse.charge_code_id == row.id,
            ChargeCodeUse.order_id == order.id,
            ChargeCodeUse.status == "reserved",
        )
        .values(status="released")
        .execution_options(synchronize_session=False)
    )
    if released.rowcount:
        await session.execute(
            update(ChargeCode)
            .where(ChargeCode.id == row.id, ChargeCode.used_count > 0)
            .values(used_count=ChargeCode.used_count - released.rowcount)
            .execution_options(synchronize_session=False)
        )
    return True


async def consume_gift_discount(session: AsyncSession, order: Order) -> None:
    await session.execute(
        update(ChargeCodeUse)
        .where(
            ChargeCodeUse.order_id == order.id,
            ChargeCodeUse.status == "reserved",
        )
        .values(status="consumed")
        .execution_options(synchronize_session=False)
    )


def _plans_label(row: ChargeCode, plan_names: Mapping[int, str]) -> str:
    plans = code_plan_ids(row)
    if not plans:
        return ALL_PLANS
    return "، ".join(plan_names[plan_id] for plan_id in sorted(plans) if plan_id in plan_names) or DELETED_PLANS


def describe_code(row: ChargeCode, plan_names: Mapping[int, str] | None = None) -> dict:
    expiry = utc(row.expires_at) if row.expires_at else None
    state = (
        "خاموش"
        if not row.is_active
        else "منقضی"
        if expiry and expiry <= datetime.now(timezone.utc)
        else "تمام‌شده"
        if row.max_uses is not None and row.used_count >= row.max_uses
        else "فعال"
    )
    types = [
        PURCHASE_TYPES[key]
        for key in (row.purchase_types or "").split(",")
        if key in PURCHASE_TYPES
    ]
    return dict(
        expiry_input=expiry_input(expiry),
        expiry_label=expiry.astimezone(TEHRAN).strftime("%Y/%m/%d %H:%M")
        if expiry
        else "بدون انقضا",
        state=state,
        purchase_label="، ".join(types) or "همه خریدها",
        plans_label=_plans_label(row, plan_names or {}),
    )


def gift_code_scope(staff: Mapping[str, Any]) -> int | None:
    """Shop whose gift codes ``staff`` may manage (``None`` = the platform shop).

    The one rule for the page and every POST route: an explicit Owner manages the
    platform codes, anyone else needs the ``gift_codes`` permission *and* a shop.
    Staff without a shop (for example PG staff) never reach another tenant's codes.
    """
    if is_platform_admin(dict(staff)):
        return None
    if not can_shop(authz_from_staff(staff), GIFT_CODES_PERMISSION):
        raise PermissionError("دسترسی مدیریت کد هدیه ندارید")
    shop_id = shop_owner_id(dict(staff))
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


async def list_gift_codes(
    session: AsyncSession, *, shop_id: int | None, limit: int = GIFT_CODE_LIST_LIMIT
) -> list[ChargeCode]:
    """Newest codes of one shop, bounded; the shop filter is applied in SQL."""
    query = select(ChargeCode).order_by(ChargeCode.id.desc()).limit(max(1, int(limit)))
    if shop_id is None:
        query = query.where(ChargeCode.reseller_id.is_(None))
    else:
        query = query.where(ChargeCode.reseller_id == int(shop_id))
    return list((await session.execute(query)).scalars().all())


async def set_gift_code_active(
    session: AsyncSession, code_id: int, *, shop_id: int | None, active: bool
) -> bool:
    """Set a code's state in one shop-scoped UPDATE; repeating the call is harmless.

    Returns ``False`` when the code does not belong to ``shop_id``.
    """
    owner = ChargeCode.reseller_id.is_(None) if shop_id is None else ChargeCode.reseller_id == int(shop_id)
    result = await session.execute(
        update(ChargeCode)
        .where(ChargeCode.id == int(code_id), owner)
        .values(is_active=bool(active))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1
