from __future__ import annotations

import logging
import secrets
import string
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BotUser,
    DiscountCode,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    ResellerProfile,
    TrialClaim,
    UserService,
    WalletTransaction,
)
from app.services.pasarguard import extract_sub_token, get_pg
from app.services.provision_gate import (
    ProvisionError,
    assert_provision_create,
    assert_provision_renew,
)
from app.services.wallet import credit_wallet, debit_wallet

logger = logging.getLogger(__name__)


async def _maybe_pay_referral_bonus(session: AsyncSession, order: Order) -> None:
    """Credit referrer wallet once after invitee's first successful purchase delivery.

    Reads ``referral_bonus`` from shop settings (panel payment tab). Skips renewals,
    reseller application fees, and zero/invalid bonus. Idempotent via wallet reason.
    """
    note = (order.note or "").strip()
    if note.startswith("renew:") or note.startswith("reseller_app:"):
        return
    buyer = order.user
    if buyer is None:
        buyer = await session.get(BotUser, order.user_id)
    if not buyer or not buyer.referred_by_id:
        return
    from app.services.users import get_setting

    raw = await get_setting(
        session, "referral_bonus", "0", reseller_id=order.reseller_id
    )
    try:
        bonus = int(str(raw or "0").replace(",", "").strip() or "0")
    except ValueError:
        bonus = 0
    if bonus <= 0:
        return
    reason = f"referral:{int(buyer.id)}"
    existing = (
        await session.execute(
            select(WalletTransaction.id)
            .where(
                WalletTransaction.user_id == int(buyer.referred_by_id),
                WalletTransaction.reason == reason,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    referrer = await session.get(BotUser, int(buyer.referred_by_id))
    if not referrer:
        return
    try:
        await credit_wallet(session, referrer, bonus, reason)
    except IntegrityError:
        # Unique (user_id, reason) — concurrent first-delivery race
        pass
    except Exception:
        logger.warning(
            "referral bonus failed buyer=%s referrer=%s",
            getattr(buyer, "id", None),
            getattr(referrer, "id", None),
            exc_info=True,
        )


async def _reseller_pg_link(
    session: AsyncSession, reseller_id: int | None
) -> tuple[str | None, int | None]:
    """Return (pg_admin_username, pg_role_id) for a reseller shop owner."""
    if not reseller_id:
        return None, None
    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_id))
        )
    ).scalar_one_or_none()
    if not profile:
        return None, None
    return (
        (profile.pg_admin_username or "").strip() or None,
        int(profile.pg_role_id) if profile.pg_role_id else None,
    )


def _random_alnum(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_username(
    prefix: str = "clk",
    suffix: str = "",
    pattern: str | None = None,
    *,
    user_id: str | int | None = None,
) -> str:
    """Build a PG username from prefix/suffix or an optional pattern.

    Pattern placeholders: ``{prefix}``, ``{random}`` (8 alnum), ``{suffix}``, ``{id}``.
    """
    random_part = _random_alnum(8)
    id_part = "" if user_id is None else str(user_id)
    prefix = (prefix or "clk").strip() or "clk"
    suffix = suffix or ""
    if pattern and pattern.strip():
        from app.services.message_variables import DOMAIN_NAMING, render_message_template

        built = render_message_template(
            pattern,
            domain=DOMAIN_NAMING,
            prefix=prefix,
            random=random_part,
            suffix=suffix,
            id=id_part,
            html=False,
        ).strip()
        if built and "{" not in built:
            return built
    base = f"{prefix}_{random_part}"
    return f"{base}{suffix}" if suffix else base


def resolve_username_naming(
    settings: dict[str, str],
    *,
    plan_prefix: str | None = None,
    plan_suffix: str | None = None,
    plan_pattern: str | None = None,
) -> tuple[str, str, str]:
    """Merge optional per-plan overrides with global settings.

    Empty / None plan fields inherit the matching global setting.
    """

    def _pick(plan_val: str | None, key: str, default: str) -> str:
        raw = (plan_val or "").strip()
        if raw:
            return raw
        return (settings.get(key) or default).strip() or default

    prefix = _pick(plan_prefix, "pg_username_prefix", "clk") or "clk"
    suffix = _pick(plan_suffix, "pg_username_suffix", "")
    pattern = _pick(
        plan_pattern,
        "pg_username_pattern",
        "{prefix}_{random}{suffix}",
    ) or "{prefix}_{random}{suffix}"
    if not pattern.strip():
        pattern = (settings.get("pg_username_vars") or "").strip() or "{prefix}_{random}{suffix}"
    return prefix, suffix, pattern


def naming_from_plan(plan: object | None) -> tuple[str | None, str | None, str | None]:
    if plan is None:
        return None, None, None
    return (
        getattr(plan, "pg_username_prefix", None),
        getattr(plan, "pg_username_suffix", None),
        getattr(plan, "pg_username_pattern", None),
    )


def parse_naming_form(
    form,
    *,
    prefix_key: str = "pg_username_prefix",
    suffix_key: str = "pg_username_suffix",
    pattern_key: str = "pg_username_pattern",
) -> tuple[str | None, str | None, str | None]:
    """Read naming fields from a web form; blank → None (inherit global)."""

    def _one(key: str, maxlen: int) -> str | None:
        raw = str(form.get(key) or "").strip()[:maxlen]
        return raw or None

    return _one(prefix_key, 64), _one(suffix_key, 64), _one(pattern_key, 255)


async def generate_pg_username(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    plan: object | None = None,
    plan_prefix: str | None = None,
    plan_suffix: str | None = None,
    plan_pattern: str | None = None,
    reseller_id: int | None = None,
) -> str:
    """Generate a Pasarguard username using plan overrides when set, else globals."""
    from app.services.users import get_all_settings

    ui = await get_all_settings(session, reseller_id=reseller_id)
    if plan is not None and plan_prefix is None and plan_suffix is None and plan_pattern is None:
        plan_prefix, plan_suffix, plan_pattern = naming_from_plan(plan)
    prefix, suffix, pattern = resolve_username_naming(
        ui,
        plan_prefix=plan_prefix,
        plan_suffix=plan_suffix,
        plan_pattern=plan_pattern,
    )
    return _random_username(
        prefix=prefix,
        suffix=suffix,
        pattern=pattern,
        user_id=user_id,
    )


async def list_active_plans(
    session: AsyncSession,
    *,
    include_trial: bool = True,
    reseller_id: int | None = None,
) -> list[Plan]:
    """Active shop catalog.

    On a reseller-owned bot (or when reseller_id is passed), only that reseller's
    plans are returned. On the platform bot, only admin/platform plans.
    """
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(Plan).where(Plan.is_active.is_(True))
    if rid:
        q = q.where(Plan.owner_reseller_id == rid)
    else:
        q = q.where(Plan.owner_reseller_id.is_(None))
    q = q.order_by(Plan.sort_order, Plan.id)
    result = await session.execute(q)
    plans = list(result.scalars().all())
    if not include_trial:
        plans = [p for p in plans if not p.is_trial]
    return plans


async def get_plan(session: AsyncSession, plan_id: int) -> Optional[Plan]:
    return await session.get(Plan, plan_id)


async def get_catalog_plan(session: AsyncSession, plan_id: int) -> Optional[Plan]:
    """Plan visible in the current shop bot context (tenant-scoped)."""
    from app.services.users import current_shop_reseller_id

    plan = await session.get(Plan, plan_id)
    if not plan:
        return None
    shop_rid = current_shop_reseller_id()
    if shop_rid:
        if int(plan.owner_reseller_id or 0) != int(shop_rid):
            return None
    elif plan.owner_reseller_id is not None:
        return None
    return plan


def _shop_reseller_id() -> int | None:
    """Always attribute orders to the current shop bot, never sticky user.reseller_id."""
    from app.services.users import current_shop_reseller_id

    rid = current_shop_reseller_id()
    return int(rid) if rid else None


async def apply_discount(session: AsyncSession, code: str | None, amount: int) -> tuple[int, str | None]:
    """Read-only discount preview (does not consume uses)."""
    if not code:
        return 0, None
    result = await session.execute(
        select(DiscountCode).where(DiscountCode.code == code.upper(), DiscountCode.is_active.is_(True))
    )
    row = result.scalar_one_or_none()
    if not row:
        return 0, None
    if row.max_uses is not None and row.used_count >= row.max_uses:
        return 0, None
    discount = int(amount * row.percent / 100)
    return discount, row.code


async def apply_discount_to_order(
    session: AsyncSession,
    order: Order,
    code: str | None,
) -> Order:
    """Reserve a discount code onto a still-payable order (consumes one use)."""
    # Only before a payment method is locked in — otherwise Payment.amount drifts.
    if order.status not in {
        OrderStatus.PENDING.value,
        OrderStatus.REJECTED.value,
    }:
        raise ValueError("این سفارش قابل تخفیف نیست")
    if order.discount_code:
        raise ValueError("روی این سفارش قبلاً تخفیف اعمال شده")
    # Base amount before any discount
    base = int(order.amount) + int(order.discount_amount or 0)
    normalized = (code or "").strip().upper()
    if normalized.startswith("LOY"):
        from app.services.loyalty import reserve_loyalty_discount

        discount, used_code = await reserve_loyalty_discount(
            session,
            user_id=int(order.user_id),
            code=normalized,
            order=order,
            base_amount=base,
        )
    else:
        discount, used_code = await _reserve_discount_code(session, code, base)
    if not used_code:
        raise ValueError("کد تخفیف نامعتبر است")
    order.discount_amount = discount
    order.discount_code = used_code
    order.amount = max(0, base - discount)
    await session.commit()
    await session.refresh(order)
    return order


async def _reserve_discount_code(
    session: AsyncSession, code: str | None, amount: int
) -> tuple[int, str | None]:
    """Atomically reserve one use at order creation (prevents max_uses races)."""
    if not code:
        return 0, None
    from sqlalchemy import or_

    normalized = code.upper().strip()
    result = await session.execute(
        select(DiscountCode).where(DiscountCode.code == normalized, DiscountCode.is_active.is_(True))
    )
    row = result.scalar_one_or_none()
    if not row:
        return 0, None
    if row.max_uses is not None and row.used_count >= row.max_uses:
        return 0, None
    claim = await session.execute(
        update(DiscountCode)
        .where(
            DiscountCode.id == row.id,
            DiscountCode.is_active.is_(True),
            or_(
                DiscountCode.max_uses.is_(None),
                DiscountCode.used_count < DiscountCode.max_uses,
            ),
        )
        .values(used_count=DiscountCode.used_count + 1)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        return 0, None
    pct = max(0, min(100, int(row.percent or 0)))
    discount = int(amount * pct / 100)
    return discount, row.code


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    plan_id: int,
    reseller_id: int | None = None,
    discount_code: str | None = None,
) -> Order:
    plan = await get_catalog_plan(session, plan_id)
    if not plan or not plan.is_active:
        raise ValueError("پلن یافت نشد")
    shop_rid = _shop_reseller_id()
    # Ignore sticky attribution from caller — shop bot context wins.
    _ = reseller_id
    if plan.is_trial:
        # One free trial per user per shop — unique claim closes the race window.
        shop_key = str(int(shop_rid)) if shop_rid is not None else "platform"
        if shop_rid is None and plan.owner_reseller_id is not None:
            raise ValueError("پلن تست این فروشگاه در دسترس نیست")
        claim = TrialClaim(user_id=user_id, shop_key=shop_key)
        session.add(claim)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("پلن تست رایگان را قبلاً دریافت کرده‌اید") from exc
    discount, used_code = await _reserve_discount_code(session, discount_code, plan.price)
    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        reseller_id=shop_rid,
        amount=max(0, plan.price - discount),
        discount_amount=discount,
        discount_code=used_code,
        status=OrderStatus.PENDING.value,
    )
    session.add(order)
    await session.flush()
    if plan.is_trial:
        shop_key = str(int(shop_rid)) if shop_rid is not None else "platform"
        row = (
            await session.execute(
                select(TrialClaim).where(
                    TrialClaim.user_id == user_id,
                    TrialClaim.shop_key == shop_key,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.order_id = order.id
    await session.commit()
    await session.refresh(order)
    return order


def calc_custom_plan_price(
    *,
    gb: float | int,
    days: int,
    price_per_gb: int,
    price_per_day: int,
) -> int:
    return max(0, int(gb) * int(price_per_gb) + int(days) * int(price_per_day))


async def create_custom_order(
    session: AsyncSession,
    *,
    user_id: int,
    data_limit_gb: float,
    duration_days: int,
    reseller_id: int | None = None,
    discount_code: str | None = None,
) -> Order:
    """Create an order for a user-chosen GB/days combo via an inactive temp Plan."""
    from app.services.users import get_all_settings, on

    ui = await get_all_settings(session)
    if not on(ui.get("custom_plan_enabled")):
        raise ValueError("پلن دلخواه فعال نیست")
    # Require at least one active catalog plan (non-trial)
    catalog = await list_active_plans(session, include_trial=False)
    if not catalog:
        raise ValueError("پلن دلخواه بدون پلن فعال در فروشگاه در دسترس نیست")

    min_gb = int(float(ui.get("custom_plan_min_gb") or 1))
    max_gb = int(float(ui.get("custom_plan_max_gb") or 500))
    min_days = int(float(ui.get("custom_plan_min_days") or 1))
    max_days = int(float(ui.get("custom_plan_max_days") or 365))
    price_per_gb = int(float(ui.get("custom_plan_price_per_gb") or 1000))
    price_per_day = int(float(ui.get("custom_plan_price_per_day") or 500))

    gb = float(data_limit_gb)
    days = int(duration_days)
    if gb < min_gb or gb > max_gb:
        raise ValueError(f"حجم باید بین {min_gb} تا {max_gb} گیگ باشد")
    if days < min_days or days > max_days:
        raise ValueError(f"مدت باید بین {min_days} تا {max_days} روز باشد")

    amount = calc_custom_plan_price(
        gb=gb,
        days=days,
        price_per_gb=price_per_gb,
        price_per_day=price_per_day,
    )
    tpl_raw = (ui.get("custom_plan_template_id") or "").strip()
    tpl_id = int(tpl_raw) if tpl_raw.isdigit() else None
    group_ids = (ui.get("custom_plan_group_ids") or "").strip() or None
    name_prefix = (ui.get("custom_plan_username_prefix") or "").strip() or None
    name_suffix = (ui.get("custom_plan_username_suffix") or "").strip() or None
    name_pattern = (ui.get("custom_plan_username_pattern") or "").strip() or None
    shop_rid = _shop_reseller_id()
    _ = reseller_id  # sticky user attribution must not override shop context
    if shop_rid:
        # Do not inherit platform PG template/groups for reseller custom plans
        from app.db.models import ResellerSetting

        own = await session.execute(
            select(ResellerSetting).where(
                ResellerSetting.reseller_user_id == int(shop_rid),
                ResellerSetting.key.in_(
                    (
                        "custom_plan_template_id",
                        "custom_plan_group_ids",
                        "custom_plan_username_prefix",
                        "custom_plan_username_suffix",
                        "custom_plan_username_pattern",
                    )
                ),
            )
        )
        own_map = {r.key: (r.value or "").strip() for r in own.scalars().all()}
        tpl_raw = own_map.get("custom_plan_template_id") or ""
        group_ids = own_map.get("custom_plan_group_ids") or None
        tpl_id = int(tpl_raw) if tpl_raw.isdigit() else None
        name_prefix = own_map.get("custom_plan_username_prefix") or None
        name_suffix = own_map.get("custom_plan_username_suffix") or None
        name_pattern = own_map.get("custom_plan_username_pattern") or None
        if not tpl_id and not group_ids:
            raise ValueError(
                "برای پلن دلخواه، تمپلیت یا گروه پاسارگارد اختصاصی فروشگاه را در تنظیمات مشخص کنید"
            )

    plan = Plan(
        name="پلن دلخواه",
        description=f"سفارشی {gb:g} گیگ / {days} روز",
        price=amount,
        duration_days=days,
        data_limit_gb=gb,
        pg_template_id=tpl_id,
        pg_group_ids=group_ids,
        pg_username_prefix=name_prefix,
        pg_username_suffix=name_suffix,
        pg_username_pattern=name_pattern,
        owner_reseller_id=shop_rid,
        is_active=False,
        is_trial=False,
        sort_order=9999,
    )
    session.add(plan)
    await session.flush()

    discount, used_code = await _reserve_discount_code(session, discount_code, amount)
    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        reseller_id=shop_rid,
        amount=max(0, amount - discount),
        discount_amount=discount,
        discount_code=used_code,
        status=OrderStatus.PENDING.value,
        note="custom",
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


def parse_wholesale_tiers(raw: str | None) -> list[dict]:
    """Parse wholesale discount tiers from settings JSON.

    Expected shape: [{"min":5,"percent":10},{"min":20,"percent":20}]
    Invalid entries are skipped. Sorted by min ascending.
    """
    import json

    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    tiers: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            mn = int(item.get("min"))
            pct = int(item.get("percent"))
        except (TypeError, ValueError):
            continue
        if mn < 1 or pct < 0 or pct > 100:
            continue
        tiers.append({"min": mn, "percent": pct})
    tiers.sort(key=lambda t: t["min"])
    return tiers


def wholesale_bounds(ui: dict | None) -> tuple[int, int]:
    ui = ui or {}
    try:
        mn = int(float(ui.get("wholesale_min_qty") or 5))
    except (TypeError, ValueError):
        mn = 5
    try:
        mx = int(float(ui.get("wholesale_max_qty") or 20))
    except (TypeError, ValueError):
        mx = 20
    mn = max(1, mn)
    mx = max(mn, mx)
    return mn, mx


def wholesale_tier_percent(qty: int, tiers: list[dict]) -> int:
    best = 0
    for t in tiers:
        if int(qty) >= int(t["min"]):
            best = int(t["percent"])
    return best


def calc_wholesale_price(*, unit_price: int, quantity: int, percent: int) -> tuple[int, int]:
    """Return (payable_amount, discount_amount) for wholesale qty."""
    gross = max(0, int(unit_price)) * max(1, int(quantity))
    pct = max(0, min(100, int(percent)))
    discount = int(gross * pct / 100)
    return max(0, gross - discount), discount


def wholesale_description(ui: dict | None) -> str:
    """Persian help text shown in the bot wholesale flow."""
    ui = ui or {}
    mn, mx = wholesale_bounds(ui)
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    lines = [
        f"حداقل خرید: <b>{mn}</b> عدد",
        f"حداکثر خرید: <b>{mx}</b> عدد",
    ]
    if tiers:
        lines.append("")
        lines.append("تخفیف پلکانی:")
        for t in tiers:
            lines.append(
                f"• خرید از <b>{t['min']}</b> عدد به بالا → <b>{t['percent']}٪</b> تخفیف"
            )
    else:
        lines.append("")
        lines.append("در حال حاضر تخفیف پلکانی تعریف نشده است.")
    return "\n".join(lines)


def order_quantity(order: Order) -> int:
    qty = getattr(order, "quantity", None)
    n: int | None
    try:
        n = int(qty) if qty is not None else None
    except (TypeError, ValueError):
        n = None
    if n is None or n < 1:
        note = (order.note or "").strip()
        if note.startswith("wholesale:"):
            try:
                n = int(note.split(":", 1)[1])
            except (TypeError, ValueError):
                n = 1
        else:
            n = 1
    return max(1, n)


async def create_wholesale_order(
    session: AsyncSession,
    *,
    user_id: int,
    plan_id: int,
    quantity: int,
    reseller_id: int | None = None,
) -> Order:
    """Create a bulk order for N identical services of one catalog plan."""
    from app.services.users import get_all_settings, on

    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        raise ValueError("فروش عمده فعال نیست")
    plan = await get_catalog_plan(session, plan_id)
    if not plan or not plan.is_active or plan.is_trial:
        raise ValueError("پلن برای فروش عمده در دسترس نیست")

    mn, mx = wholesale_bounds(ui)
    qty = int(quantity)
    if qty < mn or qty > mx:
        raise ValueError(f"تعداد باید بین {mn} تا {mx} باشد")

    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    pct = wholesale_tier_percent(qty, tiers)
    amount, discount = calc_wholesale_price(
        unit_price=plan.price, quantity=qty, percent=pct
    )
    shop_rid = _shop_reseller_id()
    _ = reseller_id
    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        reseller_id=shop_rid,
        amount=amount,
        discount_amount=discount,
        discount_code=f"wholesale:{pct}%" if pct else None,
        quantity=qty,
        status=OrderStatus.PENDING.value,
        note=f"wholesale:{qty}",
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


_PAYABLE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.REJECTED.value,
        OrderStatus.AWAITING_RECEIPT.value,
    }
)


async def _claim_payable_order(
    session: AsyncSession,
    order: Order,
    *,
    payment_method: str,
) -> Order:
    """Atomically claim a payable order → PAID. Prevents double wallet/free pay."""
    if order.status == OrderStatus.DELIVERED.value:
        return order
    order_id = int(order.id)
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_(tuple(_PAYABLE_ORDER_STATUSES)),
            )
            .values(status=OrderStatus.PAID.value, payment_method=payment_method)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        fresh = await session.get(Order, order_id)
        if fresh and fresh.status == OrderStatus.DELIVERED.value:
            return fresh
        raise ValueError("این سفارش قابل پرداخت نیست")
    await session.refresh(order)
    return order


async def manual_fulfill_unpaid_order(
    session: AsyncSession,
    order: Order,
    *,
    note: str = "web manual approve",
) -> tuple[Order, Payment]:
    """Staff override: mark an unpaid order paid and deliver without a receipt.

    Used when status is pending / awaiting_receipt (no pending payment row).
    Still respects single-delivery guards (service_id / delivered).
    """
    note_txt = (order.note or "").strip()
    is_renew_or_app = note_txt.startswith("renew:") or note_txt.startswith("reseller_app:")
    if order.status == OrderStatus.DELIVERED.value:
        raise ValueError("این سفارش قبلاً تحویل شده")
    if order.service_id and not is_renew_or_app:
        raise ValueError("این سفارش قبلاً تحویل شده")
    if order.status not in _PAYABLE_ORDER_STATUSES:
        raise ValueError("این سفارش قابل تأیید نیست")

    method = (order.payment_method or PaymentMethod.CARD.value).strip() or PaymentMethod.CARD.value
    order = await _claim_payable_order(session, order, payment_method=method)
    if order.status == OrderStatus.DELIVERED.value:
        # Concurrent path already finished
        pay = (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == order.id)
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pay is None:
            raise ValueError("این سفارش قبلاً تحویل شده")
        return order, pay

    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        amount=order.amount,
        method=method,
        status=PaymentStatus.APPROVED.value,
        review_note=(note or "").strip() or "web manual approve",
    )
    session.add(payment)
    await session.commit()
    await session.refresh(order)
    await session.refresh(payment)

    if note_txt.startswith("reseller_app:"):
        from app.services.resellers import mark_application_paid

        await mark_application_paid(session, order)
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        return order, payment
    if note_txt.startswith("renew:"):
        if not (order.service_id and order.plan_id):
            raise ValueError("سفارش تمدید ناقص است")
        service = await session.get(UserService, order.service_id)
        plan = await session.get(Plan, order.plan_id)
        if not service or not plan:
            raise ValueError("سرویس یا پلن تمدید یافت نشد")
        delivered = await apply_renewal(session, order, service, plan)
        return delivered, payment
    delivered = await deliver_order(session, order)
    return delivered, payment


def wallet_purchase_reason(order: Order) -> str:
    """Human-readable wallet debit reason (shows wholesale clearly in تراکنش‌ها)."""
    qty = order_quantity(order)
    note = (order.note or "").strip()
    if note.startswith("wholesale:") or qty > 1:
        return f"خرید عمده #{order.id} ({qty} سرویس)"
    if note.startswith("renew:"):
        return f"تمدید سرویس #{order.id}"
    if note.startswith("reseller_app:"):
        return f"هزینه نمایندگی #{order.id}"
    return f"خرید سفارش #{order.id}"


async def _resume_paid_wallet_order(session: AsyncSession, order: Order, user) -> Order:
    """Continue delivery for an already-PAID wallet order (crash recovery)."""
    if order.note and order.note.startswith("reseller_app:"):
        from app.services.resellers import mark_application_paid

        await mark_application_paid(session, order)
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        return order
    if order.note and order.note.startswith("renew:"):
        if not (order.service_id and order.plan_id):
            raise ValueError("سفارش تمدید ناقص است")
        service = await session.get(UserService, order.service_id)
        plan = await session.get(Plan, order.plan_id)
        if not service or not plan:
            raise ValueError("سرویس یا پلن تمدید یافت نشد")
        return await apply_renewal(session, order, service, plan)
    return await deliver_order(session, order)


async def pay_with_wallet(session: AsyncSession, order: Order, user) -> Order:
    if order.status == OrderStatus.DELIVERED.value:
        return order
    # Fail-closed: never debit one user for another user's order
    try:
        oid_user = int(order.user_id)
        payer = int(getattr(user, "id", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("سفارش متعلق به این کاربر نیست") from exc
    if payer <= 0 or oid_user != payer:
        raise ValueError("سفارش متعلق به این کاربر نیست")

    # Crash recovery: PAID wallet order with approved payment, delivery never finished.
    if (
        order.status == OrderStatus.PAID.value
        and (order.payment_method or "") == PaymentMethod.WALLET.value
    ):
        existing = (
            await session.execute(
                select(Payment)
                .where(
                    Payment.order_id == order.id,
                    Payment.method == PaymentMethod.WALLET.value,
                    Payment.status == PaymentStatus.APPROVED.value,
                )
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await _resume_paid_wallet_order(session, order, user)

    if order.status not in _PAYABLE_ORDER_STATUSES:
        raise ValueError("این سفارش قابل پرداخت با کیف پول نیست")

    payment: Payment | None = None
    debited = False
    order = await _claim_payable_order(
        session, order, payment_method=PaymentMethod.WALLET.value
    )
    if order.status == OrderStatus.DELIVERED.value:
        return order
    try:
        # Single DB transaction for claim + debit + payment row (commit=False debit).
        # Prevents: wallet drained / order PAID / no payment row after a mid-flow crash.
        if order.amount > 0:
            await debit_wallet(
                session,
                user,
                order.amount,
                wallet_purchase_reason(order),
                commit=False,
            )
            debited = True
        payment = Payment(
            order_id=order.id,
            user_id=user.id,
            amount=order.amount,
            method=PaymentMethod.WALLET.value,
            status=PaymentStatus.APPROVED.value,
        )
        session.add(payment)
        await session.commit()
        await session.refresh(order)
        await session.refresh(payment)
        order = await _resume_paid_wallet_order(session, order, user)
        try:
            from app.services.commerce_extras import repay_emergency_credit_if_needed

            await repay_emergency_credit_if_needed(session, user=user, order=order)
        except Exception:
            pass
        return order
    except Exception:
        # Only refund when we actually debited — never mint balance on debit failure.
        if debited and order.amount > 0:
            await credit_wallet(session, user, order.amount, f"برگشت خرید ناموفق #{order.id}")
        order.status = OrderStatus.PENDING.value
        if payment is not None:
            payment.status = PaymentStatus.REJECTED.value
            payment.review_note = payment.review_note or "delivery_failed_refunded"
        await session.commit()
        raise


async def mark_order_free_paid(session: AsyncSession, order: Order, user_id: int) -> Order:
    """Mark a zero-amount order as paid with an approved payment row."""
    if order.amount > 0:
        raise ValueError("فقط سفارش رایگان قابل علامت‌گذاری رایگان است")
    order = await _claim_payable_order(
        session, order, payment_method=PaymentMethod.WALLET.value
    )
    if order.status == OrderStatus.DELIVERED.value:
        return order
    session.add(
        Payment(
            order_id=order.id,
            user_id=user_id,
            amount=0,
            method=PaymentMethod.WALLET.value,
            status=PaymentStatus.APPROVED.value,
        )
    )
    await session.commit()
    await session.refresh(order)
    return order


async def revert_failed_free_delivery(session: AsyncSession, order: Order) -> None:
    """Undo mark_order_free_paid so the user can retry after a delivery failure."""
    order.status = OrderStatus.PENDING.value
    pays = await session.execute(
        select(Payment).where(
            Payment.order_id == order.id,
            Payment.status == PaymentStatus.APPROVED.value,
        )
    )
    for p in pays.scalars().all():
        p.status = PaymentStatus.REJECTED.value
        p.review_note = p.review_note or "delivery_failed"
    # Release trial + reserved discount so a failed first attempt does not burn them
    await _release_trial_claim_for_order(session, order)
    await _release_order_discount(session, order)
    await session.commit()


async def _release_discount_code(session: AsyncSession, code: str | None) -> None:
    """Best-effort decrement of a previously reserved discount use."""
    if not code:
        return
    normalized = code.upper().strip()
    if normalized.startswith("LOY"):
        # Loyalty path needs the order context — handled by release_loyalty_discount_for_order
        return
    await session.execute(
        update(DiscountCode)
        .where(
            DiscountCode.code == normalized,
            DiscountCode.used_count > 0,
        )
        .values(used_count=DiscountCode.used_count - 1)
        .execution_options(synchronize_session=False)
    )


async def _release_order_discount(session: AsyncSession, order: Order) -> None:
    """Release both shop DiscountCode uses and loyalty entitlements for an unpaid order."""
    code = order.discount_code
    if not code:
        return
    if str(code).upper().startswith("LOY"):
        from app.services.loyalty import release_loyalty_discount_for_order

        await release_loyalty_discount_for_order(session, order)
    else:
        await _release_discount_code(session, code)
    order.amount = int(order.amount) + int(order.discount_amount or 0)
    order.discount_amount = 0
    order.discount_code = None


def _trial_shop_key_for_order(order: Order) -> str:
    """Canonical TrialClaim.shop_key — must match create_order ('platform' / str(rid))."""
    return str(int(order.reseller_id)) if order.reseller_id is not None else "platform"


async def _release_trial_claim_for_order(session: AsyncSession, order: Order) -> None:
    """Drop unpaid trial claim so the user can retry after cancel/reject/stale cleanup."""
    claim = (
        await session.execute(
            select(TrialClaim).where(TrialClaim.order_id == int(order.id))
        )
    ).scalar_one_or_none()
    if claim is None:
        shop_key = _trial_shop_key_for_order(order)
        claim = (
            await session.execute(
                select(TrialClaim).where(
                    TrialClaim.user_id == order.user_id,
                    TrialClaim.shop_key == shop_key,
                )
            )
        ).scalar_one_or_none()
        # Only release unlinked or matching claims — never burn another order's claim.
        if claim is not None and claim.order_id is not None and int(claim.order_id) != int(order.id):
            return
    if claim is not None:
        await session.delete(claim)


async def start_card_payment(session: AsyncSession, order: Order, user_id: int) -> Payment:
    return await start_method_payment(session, order, user_id, PaymentMethod.CARD.value)


async def start_method_payment(
    session: AsyncSession,
    order: Order,
    user_id: int,
    method: str,
) -> Payment:
    """Atomically claim a payable order → AWAITING_RECEIPT (blocks wallet race)."""
    order_id = int(order.id)
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_(tuple(_PAYABLE_ORDER_STATUSES)),
            )
            .values(
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=method,
            )
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("این سفارش قابل پرداخت نیست")
    # Drop older pending payments when switching method
    old = await session.execute(
        select(Payment).where(
            Payment.order_id == order_id,
            Payment.status == PaymentStatus.PENDING.value,
        )
    )
    for prev in old.scalars().all():
        prev.status = PaymentStatus.REJECTED.value
        prev.review_note = prev.review_note or "replaced by new payment method"
    payment = Payment(
        order_id=order_id,
        user_id=user_id,
        amount=order.amount,
        method=method,
        status=PaymentStatus.PENDING.value,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    await session.refresh(order)
    return payment


def stars_amount_for_toman(amount_toman: int, toman_per_star: int) -> int:
    rate = max(1, int(toman_per_star or 1))
    return max(1, (int(amount_toman) + rate - 1) // rate)


_STALE_PENDING_BASE = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.REJECTED.value,
    }
)


async def cancel_stale_pending_orders(
    session: AsyncSession,
    *,
    older_than_hours: int,
    reseller_id: int | None = None,
    include_awaiting_approval: bool = False,
    limit: int = 500,
) -> int:
    """Cancel unpaid/abandoned orders older than TTL. Returns count cancelled.

    Uses CANCELLED (not REJECTED) so orders leave the payable set and cannot be
    completed later. Concurrent approve/pay wins via conditional UPDATE.
    """
    from datetime import datetime, timedelta, timezone

    hours = max(1, min(720, int(older_than_hours or 1)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    statuses = set(_STALE_PENDING_BASE)
    if include_awaiting_approval:
        statuses.add(OrderStatus.AWAITING_APPROVAL.value)

    q = (
        select(Order)
        .where(
            Order.status.in_(tuple(statuses)),
            Order.created_at < cutoff,
        )
        .order_by(Order.id.asc())
        .limit(max(1, min(2000, int(limit or 500))))
    )
    if reseller_id is None:
        q = q.where(Order.reseller_id.is_(None))
    else:
        q = q.where(Order.reseller_id == int(reseller_id))

    orders = list((await session.execute(q)).scalars().all())
    cancelled = 0
    for order in orders:
        order_id = int(order.id)
        with session.no_autoflush:
            claim = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status.in_(tuple(statuses)),
                )
                .values(status=OrderStatus.CANCELLED.value)
                .execution_options(synchronize_session=False)
            )
        if claim.rowcount != 1:
            continue
        # Reject sibling pending payments
        pend = await session.execute(
            select(Payment).where(
                Payment.order_id == order_id,
                Payment.status == PaymentStatus.PENDING.value,
            )
        )
        for pay in pend.scalars().all():
            pay.status = PaymentStatus.REJECTED.value
            pay.review_note = pay.review_note or "auto-cancelled stale pending order"
        if order.discount_code:
            if str(order.discount_code).upper().startswith("LOY"):
                from app.services.loyalty import release_loyalty_discount_for_order

                await release_loyalty_discount_for_order(session, order)
            else:
                await _release_discount_code(session, order.discount_code)
        # Always attempt trial release (create_order never sets note="trial:…").
        await _release_trial_claim_for_order(session, order)
        cancelled += 1

    if cancelled:
        await session.commit()
    return cancelled


async def cancel_stale_pending_for_settings(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
    force: bool = False,
) -> int:
    """Read shop/platform settings and cancel stale pending orders."""
    from app.services.users import get_all_settings, on

    ui = await get_all_settings(session, reseller_id=reseller_id)
    if not force and not on(ui.get("pending_order_cleanup_enabled", "0")):
        return 0
    try:
        hours = int(float(ui.get("pending_order_ttl_hours") or "48"))
    except (TypeError, ValueError):
        hours = 48
    include_aa = on(ui.get("pending_order_cleanup_awaiting_approval", "0"))
    return await cancel_stale_pending_orders(
        session,
        older_than_hours=hours,
        reseller_id=reseller_id,
        include_awaiting_approval=include_aa,
    )

async def attach_receipt(session: AsyncSession, payment: Payment, file_id: str) -> Payment:
    """Attach receipt only while payment is still pending (never overwrite approved/rejected)."""
    payment_id = int(payment.id)
    with session.no_autoflush:
        claim = await session.execute(
            update(Payment)
            .where(
                Payment.id == payment_id,
                Payment.status == PaymentStatus.PENDING.value,
            )
            .values(receipt_file_id=file_id, status=PaymentStatus.PENDING.value)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("این پرداخت قابل بروزرسانی نیست")
    if payment.order_id:
        await session.execute(
            update(Order)
            .where(
                Order.id == payment.order_id,
                Order.status.in_(
                    [
                        OrderStatus.PENDING.value,
                        OrderStatus.AWAITING_RECEIPT.value,
                        OrderStatus.AWAITING_APPROVAL.value,
                    ]
                ),
            )
            .values(status=OrderStatus.AWAITING_APPROVAL.value)
            .execution_options(synchronize_session=False)
        )
    await session.commit()
    await session.refresh(payment)
    return payment


async def approve_payment(session: AsyncSession, payment: Payment, reviewer_tg: int) -> Order | None:
    payment_id = int(payment.id)
    # DB-only claim: disable autoflush so a dirty in-memory status cannot
    # rewrite the row to pending before the conditional UPDATE runs.
    with session.no_autoflush:
        claim = await session.execute(
            update(Payment)
            .where(
                Payment.id == payment_id,
                Payment.status == PaymentStatus.PENDING.value,
            )
            .values(status=PaymentStatus.APPROVED.value, reviewed_by=reviewer_tg)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        fresh = await session.get(Payment, payment_id)
        if fresh and fresh.status == PaymentStatus.APPROVED.value:
            # Do not re-deliver / re-notify — caller must treat as already done.
            raise ValueError("این پرداخت قبلاً تأیید شده")
        raise ValueError("این پرداخت قابل تأیید نیست")
    await session.refresh(payment)

    if payment.is_wallet_topup:
        user = await session.get(BotUser, payment.user_id)
        if not user:
            # Never leave an APPROVED top-up without a credit. Roll the claim
            # back so panel/bot can retry after the user row is restored.
            payment.status = PaymentStatus.PENDING.value
            payment.reviewed_by = None
            payment.review_note = (
                payment.review_note or "wallet topup blocked: bot user missing"
            )
            await session.commit()
            raise ValueError(
                "کاربر پرداخت‌کننده برای شارژ کیف پول یافت نشد — تأیید لغو شد"
            )
        try:
            await credit_wallet(
                session, user, payment.amount, f"شارژ کیف پول #{payment.id}"
            )
        except Exception:
            payment.status = PaymentStatus.PENDING.value
            payment.reviewed_by = None
            payment.review_note = (
                payment.review_note or "wallet topup blocked: credit failed"
            )
            await session.commit()
            raise
        await session.commit()
        try:
            from app.services.commerce_extras import repay_emergency_credit_if_needed

            await repay_emergency_credit_if_needed(session, user=user, order=None)
        except Exception:
            pass
        return None
    order = await session.get(Order, payment.order_id)
    if not order:
        await session.commit()
        return None
    # Order already fulfilled — roll THIS payment back to rejected (do not leave
    # a second APPROVED row that double-counts revenue).
    # Also block when service_id is set even if status was tampered away from
    # delivered (cancel/re-approve must never mint a second VPN user).
    note = (order.note or "").strip()
    is_renew_or_app = note.startswith("renew:") or note.startswith("reseller_app:")
    already_fulfilled = order.status in {
        OrderStatus.DELIVERED.value,
        OrderStatus.DELIVERING.value,
    } or (bool(order.service_id) and not is_renew_or_app)
    if already_fulfilled:
        payment.status = PaymentStatus.REJECTED.value
        payment.review_note = payment.review_note or "order already delivered"
        await session.commit()
        raise ValueError("این سفارش قبلاً تحویل شده")
    # Reject sibling pending payments before committing the PAID claim
    await session.execute(
        update(Payment)
        .where(
            Payment.order_id == order.id,
            Payment.id != payment_id,
            Payment.status == PaymentStatus.PENDING.value,
        )
        .values(
            status=PaymentStatus.REJECTED.value,
            review_note="superseded by approved payment",
        )
        .execution_options(synchronize_session=False)
    )
    # Atomic PAID claim — exclude already-PAID so a concurrent wallet pay wins
    paid_claim = await session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.status.in_(
                [
                    OrderStatus.PENDING.value,
                    OrderStatus.AWAITING_RECEIPT.value,
                    OrderStatus.AWAITING_APPROVAL.value,
                ]
            ),
        )
        .values(status=OrderStatus.PAID.value)
        .execution_options(synchronize_session=False)
    )
    if paid_claim.rowcount != 1:
        await session.refresh(order)
        if order.status in {
            OrderStatus.PAID.value,
            OrderStatus.DELIVERING.value,
            OrderStatus.DELIVERED.value,
        }:
            # Another path already claimed payment — reject this approve
            payment.status = PaymentStatus.REJECTED.value
            payment.review_note = payment.review_note or "order already paid"
            await session.commit()
            raise ValueError("این سفارش قبلاً پرداخت شده")
        await session.commit()
        raise ValueError("این سفارش قابل تأیید نیست")
    await session.commit()
    await session.refresh(order)
    # Reseller application fee — no VPN delivery; move application to review queue.
    if order.note and order.note.startswith("reseller_app:"):
        from app.services.resellers import mark_application_paid

        await mark_application_paid(session, order)
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        return order
    # Renewal orders extend existing service instead of creating a new panel user.
    if order.note and order.note.startswith("renew:"):
        if not (order.service_id and order.plan_id):
            raise ValueError("سفارش تمدید ناقص است")
        service = await session.get(UserService, order.service_id)
        plan = await session.get(Plan, order.plan_id)
        if not service or not plan:
            raise ValueError("سرویس یا پلن تمدید یافت نشد")
        renewed = await apply_renewal(session, order, service, plan)
        try:
            payer = await session.get(BotUser, order.user_id)
            if payer is not None:
                from app.services.commerce_extras import repay_emergency_credit_if_needed

                await repay_emergency_credit_if_needed(
                    session, user=payer, order=renewed
                )
        except Exception:
            pass
        return renewed
    delivered = await deliver_order(session, order)
    try:
        payer = await session.get(BotUser, order.user_id)
        if payer is not None:
            from app.services.commerce_extras import repay_emergency_credit_if_needed

            await repay_emergency_credit_if_needed(
                session, user=payer, order=delivered
            )
    except Exception:
        pass
    return delivered


async def reject_payment(session: AsyncSession, payment: Payment, reviewer_tg: int, note: str = "") -> None:
    payment_id = int(payment.id)
    with session.no_autoflush:
        claim = await session.execute(
            update(Payment)
            .where(
                Payment.id == payment_id,
                Payment.status == PaymentStatus.PENDING.value,
            )
            .values(
                status=PaymentStatus.REJECTED.value,
                reviewed_by=reviewer_tg,
                review_note=note,
            )
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("این پرداخت قابل رد نیست")
    if payment.order_id:
        await session.execute(
            update(Order)
            .where(
                Order.id == payment.order_id,
                Order.status.in_(
                    [
                        OrderStatus.PENDING.value,
                        OrderStatus.AWAITING_RECEIPT.value,
                        OrderStatus.AWAITING_APPROVAL.value,
                    ]
                ),
            )
            .values(status=OrderStatus.REJECTED.value)
            .execution_options(synchronize_session=False)
        )
        rejected_order = await session.get(Order, int(payment.order_id))
        if rejected_order is not None:
            await _release_order_discount(session, rejected_order)
            await _release_trial_claim_for_order(session, rejected_order)
    await session.commit()


_MANUAL_CANCEL_STATUSES = frozenset(
    {
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.AWAITING_APPROVAL.value,
        OrderStatus.REJECTED.value,
    }
)


async def cancel_order(session: AsyncSession, order: Order, *, note: str = "") -> Order:
    """Manually cancel an unpaid order; reject sibling pending payments.

    Paid / delivering / delivered orders cannot be cancelled from the panel
    (would orphan provisioned services / skip refunds). Also blocked when a
    service was already linked or a payment was already approved — cancel then
    re-approve must never become a second delivery path.
    """
    order_id = int(order.id)
    note_txt = (order.note or "").strip()
    is_renew_or_app = note_txt.startswith("renew:") or note_txt.startswith("reseller_app:")
    if order.service_id and not is_renew_or_app:
        raise ValueError("این سفارش قبلاً تحویل شده و قابل لغو نیست")
    approved = (
        await session.execute(
            select(Payment.id)
            .where(
                Payment.order_id == order_id,
                Payment.status == PaymentStatus.APPROVED.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if approved is not None:
        raise ValueError("پرداخت تأییدشده دارد — قابل لغو نیست")
    cond = [
        Order.id == order_id,
        Order.status.in_(tuple(_MANUAL_CANCEL_STATUSES)),
    ]
    if not is_renew_or_app:
        cond.append(Order.service_id.is_(None))
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(*cond)
            .values(status=OrderStatus.CANCELLED.value)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("این سفارش قابل لغو نیست")
    reject_note = (note or "").strip() or "web order cancel"
    await session.execute(
        update(Payment)
        .where(
            Payment.order_id == order_id,
            Payment.status == PaymentStatus.PENDING.value,
        )
        .values(status=PaymentStatus.REJECTED.value, review_note=reject_note)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(order)
    if order.discount_code:
        from app.services.loyalty import release_loyalty_discount_for_order

        if str(order.discount_code).upper().startswith("LOY"):
            await release_loyalty_discount_for_order(session, order)
        else:
            await _release_discount_code(session, order.discount_code)
    await _release_trial_claim_for_order(session, order)
    await session.commit()
    await session.refresh(order)
    return order


async def reject_order(session: AsyncSession, order: Order, *, note: str = "") -> Order:
    """Reject an unpaid order (and pending payments) without going through a payment row."""
    order_id = int(order.id)
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status.in_(
                    (
                        OrderStatus.PENDING.value,
                        OrderStatus.AWAITING_RECEIPT.value,
                        OrderStatus.AWAITING_APPROVAL.value,
                    )
                ),
            )
            .values(status=OrderStatus.REJECTED.value)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        raise ValueError("این سفارش قابل رد نیست")
    reject_note = (note or "").strip() or "web order reject"
    await session.execute(
        update(Payment)
        .where(
            Payment.order_id == order_id,
            Payment.status == PaymentStatus.PENDING.value,
        )
        .values(status=PaymentStatus.REJECTED.value, review_note=reject_note)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(order)
    await _release_order_discount(session, order)
    await _release_trial_claim_for_order(session, order)
    await session.commit()
    await session.refresh(order)
    return order


async def deliver_order(session: AsyncSession, order: Order) -> Order:
    order_id = int(order.id)
    # If a prior delivery already linked services (even after status tampering),
    # never mint another PG user — just seal the order as delivered.
    if order.service_id:
        if order.status != OrderStatus.DELIVERED.value:
            order.status = OrderStatus.DELIVERED.value
            await session.commit()
            await session.refresh(order)
        return order
    prior = (
        await session.execute(
            select(UserService)
            .where(UserService.remark.like(f"order:{order_id}%"))
            .order_by(UserService.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if prior is not None:
        order.service_id = prior.id
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        return order
    # Atomic delivery claim (SQLite has no real row locks — status flip is the mutex)
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.PAID.value,
                Order.service_id.is_(None),
            )
            .values(status=OrderStatus.DELIVERING.value)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        order = (
            await session.execute(
                select(Order)
                .where(Order.id == order_id)
                .options(selectinload(Order.plan), selectinload(Order.user))
            )
        ).scalar_one()
        if order.status == OrderStatus.DELIVERED.value:
            return order
        if order.service_id:
            order.status = OrderStatus.DELIVERED.value
            await session.commit()
            await session.refresh(order)
            return order
        raise ValueError("سفارش قابل تحویل نیست")
    await session.commit()
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.plan), selectinload(Order.user))
        )
    ).scalar_one()
    plan = order.plan
    if not plan:
        order.status = OrderStatus.PAID.value
        await session.commit()
        raise ValueError("plan missing")

    async def _release_delivery_claim() -> None:
        await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.DELIVERING.value,
            )
            .values(status=OrderStatus.PAID.value)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    try:
        # Shop deliveries authenticate as the reseller's PG admin (role limits apply).
        # Platform orders use the owner client.
        if order.reseller_id:
            from app.services.pasarguard import get_pg_for_reseller

            pg = await get_pg_for_reseller(session, int(order.reseller_id))
        else:
            pg = get_pg()
        qty = order_quantity(order)

        pg_owner, pg_role_id = await _reseller_pg_link(session, order.reseller_id)
        data_limit = None
        expire = None
        if plan.data_limit_gb is not None:
            data_limit = int(plan.data_limit_gb * (1024**3))
        if plan.duration_days:
            import time

            expire = int(time.time()) + plan.duration_days * 86400

        # Reseller shop orders must always quota-check + assign ownership.
        # Creating as owner without set_owner would bypass PasarGuard max_users.
        profile: ResellerProfile | None = None
        if order.reseller_id:
            profile = (
                await session.execute(
                    select(ResellerProfile).where(ResellerProfile.user_id == order.reseller_id)
                )
            ).scalar_one_or_none()
            try:
                await assert_provision_create(
                    session,
                    reseller_user_id=int(order.reseller_id),
                    pg_admin_username=pg_owner,
                    pg_role_id=pg_role_id,
                    data_limit=data_limit,
                    expire_ts=expire,
                    from_template=bool(plan.pg_template_id),
                    quantity=qty,
                )
            except ProvisionError as e:
                raise ValueError(e.message) from e

        from app.services.pasarguard import build_user_create_payload, parse_group_ids

        group_ids = None
        if not plan.pg_template_id:
            group_ids = parse_group_ids(getattr(plan, "pg_group_ids", None))
            if not group_ids:
                raise ValueError(
                    "هیچ گروهی برای ساخت کاربر انتخاب نشده — در وب‌پنل برای پلن، گروه پاسارگارد را انتخاب کنید"
                )

        created_pg_ids: list[int] = []
        services: list[UserService] = []

        async def _create_one(index: int) -> UserService:
            username = await generate_pg_username(
                session,
                user_id=order.user_id,
                plan=plan,
                reseller_id=order.reseller_id,
            )
            note = f"PGClockBot order #{order.id}"
            if qty > 1:
                note = f"{note} ({index}/{qty})"
            if plan.pg_template_id:
                payload = {
                    "username": username,
                    "user_template_id": plan.pg_template_id,
                    "note": note,
                }
                pg_user = await pg.create_user_from_template(payload)
            else:
                pg_user = await pg.create_user(
                    build_user_create_payload(
                        username=username,
                        group_ids=group_ids or [],
                        data_limit=data_limit,
                        expire_ts=expire,
                        note=note,
                    )
                )
            pg_uid = pg_user.get("id")
            if pg_uid:
                created_pg_ids.append(int(pg_uid))
            # When creating via the shop's own PG admin, ownership is already that admin.
            # set_owner is only needed for the legacy owner-token path (no stored password).
            # With get_pg_for_reseller we skip transfer.
            if order.reseller_id and getattr(pg, "_login_username", None) is None:
                owner_name = (pg_owner or "").strip()
                if not owner_name or not pg_uid:
                    raise ValueError(
                        "کاربر ساخته شد ولی مالکیت قابل تنظیم نیست — تحویل لغو شد"
                    )
                await pg.set_owner_by_id(int(pg_uid), owner_name)
            elif order.reseller_id and not pg_uid:
                raise ValueError("ساخت کاربر پاسارگارد شناسه برنگرداند — تحویل لغو شد")

            sub_url = pg_user.get("subscription_url")
            service = UserService(
                bot_user_id=order.user_id,
                plan_id=plan.id,
                pg_user_id=pg_uid,
                pg_username=pg_user.get("username", username),
                subscription_url=sub_url,
                subscription_token=extract_sub_token(sub_url),
                remark=f"order:{order.id}",
            )
            from app.services.bot_user_admin import sync_service_quota_cache

            if isinstance(pg_user, dict):
                sync_service_quota_cache(service, pg_user)
            else:
                sync_service_quota_cache(
                    service, expire_ts=expire, data_limit_bytes=data_limit
                )
            session.add(service)
            await session.flush()
            return service

        try:
            for i in range(1, qty + 1):
                services.append(await _create_one(i))
        except Exception:
            # Roll back PG users created so far (atomic wholesale delivery)
            for pg_uid in reversed(created_pg_ids):
                try:
                    await pg.delete_user_by_id(int(pg_uid))
                except Exception:
                    pass
            # Drop flushed UserService rows so _release_delivery_claim() commit
            # cannot leave orphans pointing at deleted PG users.
            for svc in list(services):
                try:
                    await session.delete(svc)
                except Exception:
                    pass
            services.clear()
            raise

        order.service_id = services[0].id if services else None
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        try:
            await _maybe_pay_referral_bonus(session, order)
        except Exception:
            pass
        try:
            from app.services.loyalty import consume_loyalty_discount_for_order, on_order_delivered

            await consume_loyalty_discount_for_order(session, order)
            await session.commit()
            await on_order_delivered(session, order)
        except Exception:
            logger.warning("loyalty on_order_delivered failed order=%s", order.id, exc_info=True)
        return order
    except Exception:
        try:
            await _release_delivery_claim()
        except Exception:
            pass
        raise


async def create_wallet_topup(
    session: AsyncSession,
    user_id: int,
    amount: int,
    *,
    method: str = PaymentMethod.CARD.value,
) -> Payment:
    if amount < 1000:
        raise ValueError("حداقل مبلغ شارژ ۱۰۰۰ است")
    # Soft ceiling to prevent absurd pending top-ups / receipt spam
    if amount > 500_000_000:
        raise ValueError("مبلغ شارژ بیش از حد مجاز است")
    payment = Payment(
        user_id=user_id,
        amount=amount,
        method=method,
        status=PaymentStatus.PENDING.value,
        is_wallet_topup=True,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    return payment


async def renew_service_with_plan(
    session: AsyncSession,
    *,
    user_id: int,
    service: UserService,
    plan: Plan,
) -> Order:
    """Create a pending renewal order. Caller shows pay_methods (or uses pay_with_wallet)."""
    if not plan or not plan.is_active:
        raise ValueError("پلن یافت نشد")
    if plan.is_trial:
        raise ValueError("پلن تست برای تمدید مجاز نیست")
    shop_rid = _shop_reseller_id()
    if shop_rid:
        if int(plan.owner_reseller_id or 0) != int(shop_rid):
            raise ValueError("این پلن در این فروشگاه موجود نیست")
    elif plan.owner_reseller_id is not None:
        raise ValueError("این پلن در این فروشگاه موجود نیست")
    if service.bot_user_id != user_id:
        raise ValueError("سرویس متعلق به شما نیست")

    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        amount=plan.price,
        status=OrderStatus.PENDING.value,
        note=f"renew:{service.id}",
        service_id=service.id,
        reseller_id=shop_rid,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def apply_renewal(session: AsyncSession, order: Order, service: UserService, plan: Plan) -> Order:
    order_id = int(order.id)
    # Atomic renewal mutex (same pattern as deliver_order)
    with session.no_autoflush:
        claim = await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.PAID.value,
            )
            .values(status=OrderStatus.DELIVERING.value)
            .execution_options(synchronize_session=False)
        )
    if claim.rowcount != 1:
        await session.refresh(order)
        if order.status == OrderStatus.DELIVERED.value:
            return order
        raise ValueError("سفارش تمدید قابل پردازش نیست")
    await session.commit()
    await session.refresh(order)

    async def _release_renewal_claim() -> None:
        await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.DELIVERING.value,
            )
            .values(status=OrderStatus.PAID.value)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    try:
        if order.reseller_id:
            from app.services.pasarguard import get_pg_for_reseller

            pg = await get_pg_for_reseller(session, int(order.reseller_id))
        else:
            pg = get_pg()
        if not service.pg_user_id:
            raise ValueError("service has no panel user")

        pg_owner, pg_role_id = await _reseller_pg_link(session, order.reseller_id)
        data_limit = int(plan.data_limit_gb * (1024**3)) if plan.data_limit_gb is not None else None
        expire = None
        if plan.duration_days:
            import time

            expire = int(time.time()) + plan.duration_days * 86400

        if order.reseller_id:
            try:
                await assert_provision_renew(
                    session,
                    reseller_user_id=int(order.reseller_id),
                    pg_admin_username=pg_owner,
                    pg_role_id=pg_role_id,
                    data_limit=data_limit,
                    expire_ts=expire,
                    from_template=bool(plan.pg_template_id),
                )
            except ProvisionError as e:
                raise ValueError(e.message) from e

        if plan.pg_template_id:
            pg_user = await pg.modify_user_with_template(
                service.pg_user_id,
                {"user_template_id": plan.pg_template_id},
            )
        else:
            pg_user = await pg.modify_user_by_id(
                service.pg_user_id,
                {
                    "status": "active",
                    "data_limit": data_limit,
                    "expire": expire,
                },
            )
        sub_url = pg_user.get("subscription_url") or service.subscription_url
        service.subscription_url = sub_url
        service.subscription_token = extract_sub_token(sub_url)
        service.plan_id = plan.id
        service.notified_expire = False
        service.notified_traffic = False
        from app.services.bot_user_admin import sync_service_quota_cache

        if isinstance(pg_user, dict):
            sync_service_quota_cache(service, pg_user)
        else:
            sync_service_quota_cache(
                service, expire_ts=expire, data_limit_bytes=data_limit
            )
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        try:
            from app.services.loyalty import consume_loyalty_discount_for_order, on_order_delivered

            await consume_loyalty_discount_for_order(session, order)
            await session.commit()
            await on_order_delivered(session, order)
        except Exception:
            logger.warning("loyalty on_order_delivered renew failed order=%s", order.id, exc_info=True)
        return order
    except Exception:
        try:
            await _release_renewal_claim()
        except Exception:
            pass
        raise
