from __future__ import annotations

import json
import logging
import math
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services.customer_identity import optional_legacy_customer
from app.services.catalog_pricing import PriceTarget, active_discounts, annotate_prices, assert_expected_price, displayed_quote, quote_price, quote_product, sale_price
from app.db.models import (
    BotUser,
    ChargeCode,
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
from app.services.open_psp_checkouts import open_psp_checkouts, reusable_psp_payment
from app.services.pasarguard import extract_sub_token, get_pg, parse_group_ids, user_subscription_url
from app.services.purchase_contact import CONTACT_REQUIRED_MESSAGE, purchase_contact_needed, require_purchase_contact
from app.services.shop_membership import ensure_member
from app.services.provision_gate import (
    ProvisionError,
    assert_provision_create,
    assert_provision_renew,
    hold_limit_change,
    hold_plan_create,
    settle_failed_hold,
    template_limit_if_gated,
)
from app.services.volume_allocation import (
    HeldVolume,
    VolumeAllocationError,
    assert_volume_delta_allowed,
    release_volume_hold,
)
from app.services.wallet import credit_wallet, debit_wallet

logger = logging.getLogger(__name__)


def _order_note(order: Order | None) -> str:
    return ((order.note if order else None) or "").strip()


def is_mutation_order_note(note: str | None) -> bool:
    """True when ``service_id`` is a target to mutate, not a newly minted service."""
    n = (note or "").strip()
    return (
        n.startswith("renew:")
        or n.startswith("reseller_app:")
        or n.startswith("svc_addon:")
    )


def is_addon_order_note(note: str | None) -> bool:
    return (note or "").strip().startswith("svc_addon:")


async def fulfill_paid_order(session: AsyncSession, order: Order) -> Order:
    """Route a PAID order to renew / addon / reseller_app / new-mint delivery.

    Callers that previously used ``deliver_order`` for PAID recovery must use
    this helper — addon/renew orders already have ``service_id`` set and
    ``deliver_order`` would falsely seal them as delivered.
    """
    note = _order_note(order)
    if note.startswith("reseller_app:"):
        from app.services.resellers import mark_application_paid

        await mark_application_paid(session, order)
        order.status = OrderStatus.DELIVERED.value
        await session.commit()
        await session.refresh(order)
        return order
    if note.startswith("renew:"):
        if not (order.service_id and order.plan_id):
            raise ValueError("سفارش تمدید ناقص است")
        service = await session.get(UserService, order.service_id)
        plan = await session.get(Plan, order.plan_id)
        if not service or not plan:
            raise ValueError("سرویس یا پلن تمدید یافت نشد")
        return await apply_renewal(session, order, service, plan, reset_traffic=note.endswith(":auto"))
    if is_addon_order_note(note):
        from app.services.service_addons import apply_service_addon

        return await apply_service_addon(session, order)
    return await deliver_order(session, order)


async def _maybe_pay_referral_bonus(session: AsyncSession, order: Order) -> None:
    """Credit referrer wallet once after invitee's first successful purchase delivery.

    Reads ``referral_bonus`` from the same shop scope as the order. Credits the
    matching purse (shop wallet or platform) so shop bonuses cannot mint
    spendable platform balance. Skips renewals, reseller fees, trials, and
    zero-amount orders. Idempotent via wallet reason.
    """
    note = _order_note(order)
    if is_mutation_order_note(note):
        return
    if int(order.amount or 0) <= 0:
        return
    # Prefer already-loaded relationship; never trigger async lazy-load.
    buyer = order.__dict__.get("user")
    if buyer is None:
        buyer = await session.get(BotUser, order.user_id)
    if not buyer or not buyer.referred_by_id:
        return
    if order.plan_id:
        from app.db.models import Plan

        plan = await session.get(Plan, int(order.plan_id))
        if plan is not None and bool(getattr(plan, "is_trial", False)):
            return
    from app.services.users import get_setting

    shop_id = int(order.reseller_id) if order.reseller_id else None
    raw = await get_setting(
        session, "referral_bonus", "0", reseller_id=shop_id
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
        await credit_wallet(
            session, referrer, bonus, reason, shop_id=shop_id
        )
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


def _sanitize_naming_username(raw: str | None, *, fallback: str) -> str:
    """Strip @ and keep characters safe for Pasarguard usernames."""
    cleaned = "".join(
        ch for ch in (raw or "").strip().lstrip("@") if ch.isalnum() or ch in "._-"
    )
    return cleaned or fallback


def _random_username(
    prefix: str = "clk",
    suffix: str = "",
    pattern: str | None = None,
    *,
    user_id: str | int | None = None,
    username: str | None = None,
    plan_volume: str = "unlimited",
    plan_unit: str = "GB",
) -> str:
    """Build a PG username from prefix/suffix or an optional pattern.

    Pattern placeholders come from the shared naming-domain catalog.

    ``user_id`` fills ``{id}``. For order delivery callers pass ``order.id``
    (matches UI / message_variables catalog: شناسه سفارش).  That value must
    not be used as a ``BotUser`` primary key — pass ``buyer_id`` / ``user``
    separately for ``{username}``.
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
            username=_sanitize_naming_username(username, fallback=id_part),
            plan_volume=plan_volume,
            plan_unit=plan_unit,
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
    buyer_id: int | None = None,
    plan: object | None = None,
    plan_prefix: str | None = None,
    plan_suffix: str | None = None,
    plan_pattern: str | None = None,
    reseller_id: int | None = None,
    user: BotUser | None = None,
) -> str:
    """Generate a Pasarguard username using plan overrides when set, else globals.

    ``user_id`` fills ``{id}`` (order id on delivery; bot-user id on manual
    assign).  ``buyer_id`` / ``user`` resolve ``{username}`` — never look up
    ``BotUser`` by the naming ``{id}`` value when ``buyer_id`` is set.
    """
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
    # Prefer explicit buyer PK / preloaded buyer; fall back to user_id only when
    # buyer_id is omitted (manual-assign path where user_id is BotUser.id).
    if user is None and "{username}" in pattern:
        lookup_pk = buyer_id if buyer_id is not None else user_id
        if lookup_pk is not None:
            user = await session.get(BotUser, lookup_pk)
    volume = getattr(plan, "data_limit_gb", None)
    fallback_id = "" if user_id is None else str(user_id)
    tg_fallback = str(getattr(user, "telegram_id", None) or fallback_id)
    return _random_username(
        prefix=prefix,
        suffix=suffix,
        pattern=pattern,
        user_id=user_id,
        username=getattr(user, "username", None) or tg_fallback,
        plan_volume=f"{volume:g}" if volume is not None and volume > 0 else "unlimited",
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
    await annotate_prices(session, plans, shop_id=rid)
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
    await quote_product(session, plan, shop_id=shop_rid)
    return plan


def is_customer_order_in_current_shop(order: Order | None, user_id: int) -> bool:
    """True when the order is this customer's and was placed in the bot being used.

    A payment button can outlive the bot it was shown in; paying from another
    shop's bot would send the money to that shop's card, gateway or review queue.
    """
    return (
        order is not None
        and int(order.user_id) == int(user_id)
        and int(order.reseller_id or 0) == int(_shop_reseller_id() or 0)
    )


def _shop_reseller_id() -> int | None:
    """Always attribute orders to the current shop bot, never sticky user.reseller_id."""
    from app.services.users import current_shop_reseller_id

    rid = current_shop_reseller_id()
    return int(rid) if rid else None


def _legacy_code_shop(order: Order | None) -> int | None:
    """The shop a legacy ``DiscountCode`` would discount; those codes have no shop and are the platform's."""
    if order is not None:
        return int(order.reseller_id) if order.reseller_id else None
    return _shop_reseller_id()


async def apply_discount(session: AsyncSession, code: str | None, amount: int, *, order: Order | None = None) -> tuple[int, str | None]:
    """Read-only discount preview (does not consume uses)."""
    if not code:
        return 0, None
    normalized = "".join(code.split()).upper()
    gift = (await session.execute(select(ChargeCode).where(ChargeCode.code == normalized))).scalar_one_or_none()
    if gift:
        from app.services.gift_codes import preview_gift_discount

        return await preview_gift_discount(session, gift, amount, order=order, shop_id=_shop_reseller_id())
    if _legacy_code_shop(order) is not None:
        return 0, None
    result = await session.execute(
        select(DiscountCode).where(DiscountCode.code == normalized, DiscountCode.is_active.is_(True))
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
    normalized = "".join((code or "").split()).upper()
    async with session.begin_nested():
        # Serialize two discount submissions for the same payable order.
        claim = await session.execute(update(Order).where(
            Order.id == order.id, Order.discount_code.is_(None),
            Order.status.in_((OrderStatus.PENDING.value, OrderStatus.REJECTED.value)),
        ).values(amount=Order.amount).execution_options(synchronize_session=False))
        if claim.rowcount != 1:
            raise ValueError("این سفارش قابل تخفیف نیست یا قبلاً تخفیف گرفته است")
        await session.refresh(order)
        base = int(order.amount) + int(order.discount_amount or 0)
        if normalized.startswith("LOY"):
            from app.services.loyalty import reserve_loyalty_discount

            discount, used_code = await reserve_loyalty_discount(
                session, user_id=int(order.user_id), code=normalized,
                order=order, base_amount=base,
            )
        else:
            discount, used_code = await _reserve_discount_code(session, code, base, order=order)
        if not used_code:
            raise ValueError("کد تخفیف نامعتبر است")
        order.discount_amount = discount
        order.discount_code = used_code
        order.amount = max(0, base - discount)
        await session.flush()
    await session.commit()
    await session.refresh(order)
    return order


async def _reserve_discount_code(
    session: AsyncSession, code: str | None, amount: int, *, order: Order | None = None
) -> tuple[int, str | None]:
    """Atomically reserve one use at order creation (prevents max_uses races)."""
    if not code:
        return 0, None
    from sqlalchemy import or_

    normalized = "".join(code.split()).upper()
    gift = (await session.execute(select(ChargeCode).where(ChargeCode.code == normalized))).scalar_one_or_none()
    if gift:
        from app.services.gift_codes import reserve_gift_discount

        if order is None:
            return 0, None
        return await reserve_gift_discount(session, gift, order, amount)
    if _legacy_code_shop(order) is not None:
        return 0, None
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
    trial_phone_hash: str | None = None,
    trial_telegram_id: int | None = None,
    expected_price: int | None = None,
) -> Order:
    plan = await get_catalog_plan(session, plan_id)
    if not plan or not plan.is_active:
        raise ValueError("پلن یافت نشد")
    assert_expected_price(displayed_quote(plan), expected_price)
    shop_rid = _shop_reseller_id()
    # Ignore sticky attribution from caller — shop bot context wins.
    _ = reseller_id
    if not plan.is_trial:
        await require_purchase_contact(session, user_id)
    if plan.is_trial:
        # One free trial per user per shop — unique claim closes the race window.
        shop_key = str(int(shop_rid)) if shop_rid is not None else "platform"
        if shop_rid is None and plan.owner_reseller_id is not None:
            raise ValueError("پلن تست این فروشگاه در دسترس نیست")
        from app.services.users import get_all_settings, on

        ui = await get_all_settings(session, reseller_id=shop_rid)
        phone_hash = (trial_phone_hash or "").strip() or None
        tg_id = int(trial_telegram_id) if trial_telegram_id else None
        if on(ui.get("trial_require_contact")) and not phone_hash:
            raise ValueError("برای دریافت تست باید شماره تماس خود را تأیید کنید")
        if on(ui.get("trial_require_iran_phone")) and not phone_hash:
            raise ValueError("برای دریافت تست باید شماره موبایل ایران را تأیید کنید")
        claim = TrialClaim(
            user_id=user_id,
            shop_key=shop_key,
            phone_hash=phone_hash,
            telegram_id=tg_id,
        )
        try:
            async with session.begin_nested():
                session.add(claim)
                await session.flush()
        except IntegrityError as exc:
            raise ValueError("پلن تست رایگان را قبلاً دریافت کرده‌اید") from exc
    await _assert_plan_volume_available(session, shop_rid, plan)
    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        reseller_id=shop_rid,
        amount=sale_price(plan),
        discount_amount=0,
        status=OrderStatus.PENDING.value,
    )
    session.add(order)
    await session.flush()
    await ensure_member(session, user_id, shop_rid)
    try:
        discount, used_code = await _reserve_discount_code(session, discount_code, sale_price(plan), order=order)
    except ValueError:
        await session.rollback()
        raise
    order.amount = max(0, sale_price(plan) - discount)
    order.discount_amount = discount
    order.discount_code = used_code
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
    # Ceil fractional GB so e.g. 1.9 is billed as 2 (never undercharge).
    gb_units = max(0, math.ceil(float(gb))) if float(gb) > 0 else 0
    return max(0, gb_units * int(price_per_gb) + int(days) * int(price_per_day))


CUSTOM_ORDER_NOTE = "custom"
CUSTOM_PLAN_UNCONFIGURED = "پلن دلخواه فعلاً در دسترس نیست؛ لطفاً یکی از پلن‌های آماده را انتخاب کنید"


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
    await require_purchase_contact(session, user_id)
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
        raise ValueError(f"حجم باید بین {min_gb} تا {max_gb} گیگابایت باشد")
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
    elif not tpl_id and not parse_group_ids(group_ids):
        # Delivery needs a template or groups; refuse before payment, not after it.
        raise ValueError(CUSTOM_PLAN_UNCONFIGURED)

    plan = Plan(
        name="پلن دلخواه",
        description=f"سفارشی {gb:g} گیگابایت / {days} روز",
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

    amount = quote_price(PriceTarget(amount, kind="custom"), await active_discounts(session, shop_id=shop_rid)).price
    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        reseller_id=shop_rid,
        amount=amount,
        discount_amount=0,
        status=OrderStatus.PENDING.value,
        note=CUSTOM_ORDER_NOTE,
    )
    session.add(order)
    await session.flush()
    await ensure_member(session, user_id, shop_rid)
    try:
        discount, used_code = await _reserve_discount_code(session, discount_code, amount, order=order)
    except ValueError:
        await session.rollback()
        raise
    order.amount = max(0, amount - discount)
    order.discount_amount = discount
    order.discount_code = used_code
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


def plan_limit_bytes(plan: Plan) -> int:
    """Per-user limit of a non-template plan in bytes; 0 is unlimited (same rounding as delivery)."""
    return int(plan.data_limit_gb * (1024**3)) if plan.data_limit_gb is not None else 0


async def _assert_plan_volume_available(
    session: AsyncSession, shop_rid: int | None, plan: Plan, *, quantity: int = 1
) -> None:
    """Refuse an order the shop cannot deliver within its volume ceiling; delivery holds it again.

    A template plan's volume lives in the panel and is read at delivery, so it is not checked here.
    """
    if shop_rid is None or plan.pg_template_id:
        return
    per_user = plan_limit_bytes(plan)
    try:
        await assert_volume_delta_allowed(
            session, shop_id=int(shop_rid), delta_bytes=per_user * max(1, int(quantity)), audience="customer",
        )
    except VolumeAllocationError as e:
        raise ValueError(e.user_message()) from e


async def _hold_delivery_volume(
    session: AsyncSession, order: Order, plan: Plan, pg, *, data_limit: int | None, quantity: int
) -> HeldVolume | None:
    """Hold the volume of a shop delivery before its panel calls; None for the platform."""
    try:
        return await hold_plan_create(
            session, shop_id=order.reseller_id, pg=pg, template_id=plan.pg_template_id,
            data_limit=data_limit, quantity=quantity, key=f"order:{int(order.id)}", audience="customer",
        )
    except ProvisionError as e:
        raise ValueError(e.message) from e


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
    await require_purchase_contact(session, user_id)
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
        unit_price=sale_price(plan), quantity=qty, percent=pct
    )
    shop_rid = _shop_reseller_id()
    _ = reseller_id
    await _assert_plan_volume_available(session, shop_rid, plan, quantity=qty)
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
    await ensure_member(session, user_id, shop_rid)
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
    note_txt = _order_note(order)
    is_mutation = is_mutation_order_note(note_txt)
    if order.status == OrderStatus.DELIVERED.value:
        raise ValueError("این سفارش قبلاً تحویل شده")
    if order.service_id and not is_mutation:
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
    if is_addon_order_note(note_txt):
        from app.services.service_addons import apply_service_addon

        delivered = await apply_service_addon(session, order)
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
    if note.startswith("svc_addon:"):
        return f"افزونه سرویس #{order.id}"
    if note.startswith("reseller_app:"):
        return f"هزینه نمایندگی #{order.id}"
    return f"خرید سفارش #{order.id}"


async def _resume_paid_wallet_order(session: AsyncSession, order: Order, user) -> Order:
    """Continue delivery for an already-PAID wallet order (crash recovery)."""
    _ = user
    return await fulfill_paid_order(session, order)


async def _require_checkout_contact(session: AsyncSession, order: Order) -> None:
    if is_mutation_order_note(order.note) or order.plan_id is None:
        return
    if not await purchase_contact_needed(session, order.user_id, shop_key=order.reseller_id or 0):
        return
    is_trial = await session.scalar(select(Plan.is_trial).where(Plan.id == order.plan_id))
    if not is_trial:
        raise ValueError(CONTACT_REQUIRED_MESSAGE)


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
    await _require_checkout_contact(session, order)

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
        shop_id = int(order.reseller_id) if order.reseller_id else None
        if order.amount > 0:
            await debit_wallet(
                session,
                user,
                order.amount,
                wallet_purchase_reason(order),
                shop_id=shop_id,
                commit=False,
            )
            debited = True
        payment = Payment(
            order_id=order.id,
            user_id=user.id,
            amount=order.amount,
            method=PaymentMethod.WALLET.value,
            status=PaymentStatus.APPROVED.value,
            wallet_shop_id=shop_id,
        )
        session.add(payment)
        await session.commit()
        await session.refresh(order)
        await session.refresh(payment)
        return await _resume_paid_wallet_order(session, order, user)
    except Exception as exc:
        from app.services.service_renewals import RenewalPendingReview

        if isinstance(exc, RenewalPendingReview):
            # A lost panel response is not evidence that the paid renewal failed.
            raise
        if getattr(order, "renewal_snapshot", None):
            await session.refresh(order)
            await session.refresh(user)
        # Only refund when we actually debited — never mint balance on debit failure.
        if debited and order.amount > 0:
            shop_id = int(order.reseller_id) if order.reseller_id else None
            await credit_wallet(
                session,
                user,
                order.amount,
                f"برگشت خرید ناموفق #{order.id}",
                shop_id=shop_id,
            )
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
    if order.status == OrderStatus.DELIVERED.value:
        return order
    await _require_checkout_contact(session, order)
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
    if getattr(order, "service_mutation_pending", False):
        return
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


async def _release_discount_code(session: AsyncSession, code: str | None, *, order: Order | None = None) -> None:
    """Best-effort decrement of a previously reserved discount use."""
    if not code:
        return
    if order is not None:
        from app.services.gift_codes import release_gift_discount

        if await release_gift_discount(session, order):
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
        await _release_discount_code(session, code, order=order)
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
    await _require_checkout_contact(session, order)
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
    pending = list(
        (
            await session.execute(
                select(Payment).where(
                    Payment.order_id == order_id,
                    Payment.status == PaymentStatus.PENDING.value,
                )
            )
        ).scalars().all()
    )
    checkouts = await open_psp_checkouts(session, [int(p.id) for p in pending])
    if method == PaymentMethod.PSP.value:
        reused = reusable_psp_payment(
            pending, checkouts, amount=int(order.amount), now=datetime.now(timezone.utc)
        )
        if reused is not None:
            await session.commit()
            await session.refresh(reused)
            await session.refresh(order)
            return reused
    # Drop older pending payments when switching method, except gateway payments
    # whose link may still be paid; approve_payment supersedes them if another wins.
    for prev in pending:
        if int(prev.id) in checkouts:
            continue
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
                await _release_discount_code(session, order.discount_code, order=order)
        # Always attempt trial release (create_order never sets note="trial:…").
        await _release_trial_claim_for_order(session, order)
        # Unblock reseller apply if this was an unpaid agency checkout.
        if (order.note or "").startswith("reseller_app:"):
            from app.services.resellers import cancel_application_for_order

            await cancel_application_for_order(
                session,
                order,
                reason="auto-cancelled stale pending order",
                commit=False,
            )
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

async def attach_receipt(
    session: AsyncSession, payment: Payment, file_id: str, *, first_only: bool = False
) -> Payment:
    """Attach receipt only while payment is still pending (never overwrite approved/rejected).

    ``first_only`` refuses a payment that already has a receipt, so two concurrent
    uploads cannot both reach the reviewers.
    """
    payment_id = int(payment.id)
    conditions = [Payment.id == payment_id, Payment.status == PaymentStatus.PENDING.value]
    if first_only:
        conditions.append(Payment.receipt_file_id.is_(None))
    with session.no_autoflush:
        claim = await session.execute(
            update(Payment)
            .where(*conditions)
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


def _wallet_topup_credit_reason(payment_id: int) -> str:
    return f"شارژ کیف پول #{int(payment_id)}"


async def _wallet_topup_already_credited(
    session: AsyncSession, payment: Payment
) -> bool:
    """True when the idempotent top-up ledger row for this payment exists."""
    reason = _wallet_topup_credit_reason(int(payment.id))
    topup_shop = (
        int(payment.wallet_shop_id)
        if getattr(payment, "wallet_shop_id", None)
        else None
    )
    q = select(WalletTransaction.id).where(
        WalletTransaction.user_id == int(payment.user_id),
        WalletTransaction.reason == reason,
        WalletTransaction.amount == int(payment.amount),
    )
    if topup_shop is None:
        q = q.where(WalletTransaction.reseller_id.is_(None))
    else:
        q = q.where(WalletTransaction.reseller_id == topup_shop)
    row = (
        await session.execute(q.limit(1))
    ).scalar_one_or_none()
    return row is not None


async def _resume_approved_payment(
    session: AsyncSession, payment: Payment
) -> Order | None:
    """Continue after APPROVED claim when credit/delivery never finished.

    Security invariants:
    - Never credit a wallet top-up twice (ledger reason check).
    - Never mint a second VPN user for an already-delivered order.
    - APPROVED + incomplete work is retriable; APPROVED + complete is terminal.
    """
    if payment.is_wallet_topup:
        if await _wallet_topup_already_credited(session, payment):
            return None
        user = await session.get(BotUser, payment.user_id)
        if not user:
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
            topup_shop = (
                int(payment.wallet_shop_id)
                if getattr(payment, "wallet_shop_id", None)
                else None
            )
            await credit_wallet(
                session,
                user,
                payment.amount,
                _wallet_topup_credit_reason(int(payment.id)),
                shop_id=topup_shop,
            )
        except Exception:
            payment.status = PaymentStatus.PENDING.value
            payment.reviewed_by = None
            payment.review_note = (
                payment.review_note or "wallet topup blocked: credit failed"
            )
            await session.commit()
            raise
        return None

    if not payment.order_id:
        raise ValueError("این پرداخت قبلاً تأیید شده")
    order = await session.get(Order, payment.order_id)
    if not order:
        raise ValueError("این پرداخت قبلاً تأیید شده")

    note = _order_note(order)
    is_mutation = is_mutation_order_note(note)
    if order.status == OrderStatus.DELIVERED.value or (
        bool(order.service_id) and not is_mutation
    ):
        # Truly complete — do not re-run delivery / notifications as a new approve.
        raise ValueError("این پرداخت قبلاً تأیید شده")

    # Heal mid-flight / crashed delivery back to PAID so fulfill can claim again.
    if order.status == OrderStatus.DELIVERING.value and not (
        bool(order.service_id) and not is_mutation
    ):
        with session.no_autoflush:
            await session.execute(
                update(Order)
                .where(
                    Order.id == int(order.id),
                    Order.status == OrderStatus.DELIVERING.value,
                )
                .values(status=OrderStatus.PAID.value)
                .execution_options(synchronize_session=False)
            )
        await session.commit()
        await session.refresh(order)

    if order.status in {
        OrderStatus.PENDING.value,
        OrderStatus.AWAITING_RECEIPT.value,
        OrderStatus.AWAITING_APPROVAL.value,
    }:
        # Payment already APPROVED but order never flipped to PAID (crash between
        # claims). Finish the PAID claim idempotently, then fulfill.
        await session.execute(
            update(Payment)
            .where(
                Payment.order_id == order.id,
                Payment.id != int(payment.id),
                Payment.status == PaymentStatus.PENDING.value,
            )
            .values(
                status=PaymentStatus.REJECTED.value,
                review_note="superseded by approved payment",
            )
            .execution_options(synchronize_session=False)
        )
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
            if order.status not in {
                OrderStatus.PAID.value,
                OrderStatus.DELIVERING.value,
            }:
                raise ValueError("این سفارش قابل تأیید نیست")
        else:
            await session.commit()
            await session.refresh(order)

    if order.status == OrderStatus.PAID.value:
        return await fulfill_paid_order(session, order)

    raise ValueError("این پرداخت قبلاً تأیید شده")


async def _sync_payment_review_messages(
    session: AsyncSession, payment_id: int, *, bot=None
) -> None:
    try:
        from app.services.payment_review_messages import sync_review_messages

        async with session.begin_nested():
            await sync_review_messages(session, payment_id, bot=bot)
    except Exception:
        logger.warning("payment review sync failed payment=%s", payment_id, exc_info=True)


async def approve_payment(
    session: AsyncSession, payment: Payment, reviewer_tg: int, *, bot=None
) -> Order | None:
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
        # Force DB truth — expire_on_commit=False can keep a stale PENDING identity.
        fresh = (
            await session.execute(
                select(Payment)
                .where(Payment.id == payment_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if fresh and fresh.status == PaymentStatus.APPROVED.value:
            await _sync_payment_review_messages(session, payment_id, bot=bot)
            # Approved but credit/delivery may still be incomplete — resume safely.
            return await _resume_approved_payment(session, fresh)
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
            topup_shop = (
                int(payment.wallet_shop_id)
                if getattr(payment, "wallet_shop_id", None)
                else None
            )
            await credit_wallet(
                session,
                user,
                payment.amount,
                _wallet_topup_credit_reason(int(payment.id)),
                shop_id=topup_shop,
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
        await _sync_payment_review_messages(session, payment_id, bot=bot)
        return None
    order = await session.get(Order, payment.order_id)
    if not order:
        await session.commit()
        await _sync_payment_review_messages(session, payment_id, bot=bot)
        return None
    # Order already fulfilled — roll THIS payment back to rejected (do not leave
    # a second APPROVED row that double-counts revenue).
    # Also block when service_id is set even if status was tampered away from
    # delivered (cancel/re-approve must never mint a second VPN user).
    note = _order_note(order)
    is_mutation = is_mutation_order_note(note)
    already_fulfilled = order.status in {
        OrderStatus.DELIVERED.value,
        OrderStatus.DELIVERING.value,
    } or (bool(order.service_id) and not is_mutation)
    if already_fulfilled:
        payment.status = PaymentStatus.REJECTED.value
        payment.review_note = payment.review_note or "order already delivered"
        await session.commit()
        await _sync_payment_review_messages(session, payment_id, bot=bot)
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
            await _sync_payment_review_messages(session, payment_id, bot=bot)
            raise ValueError("این سفارش قبلاً پرداخت شده")
        await session.commit()
        raise ValueError("این سفارش قابل تأیید نیست")
    await session.commit()
    await session.refresh(order)
    await _sync_payment_review_messages(session, payment_id, bot=bot)
    return await fulfill_paid_order(session, order)


async def reject_payment(
    session: AsyncSession, payment: Payment, reviewer_tg: int, note: str = "", *, bot=None
) -> None:
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
        await _sync_payment_review_messages(session, payment_id, bot=bot)
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
    await _sync_payment_review_messages(session, payment_id, bot=bot)


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
    note_txt = _order_note(order)
    is_mutation = is_mutation_order_note(note_txt)
    if order.service_id and not is_mutation:
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
    if not is_mutation:
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
            await _release_discount_code(session, order.discount_code, order=order)
    await _release_trial_claim_for_order(session, order)
    if (order.note or "").startswith("reseller_app:"):
        from app.services.resellers import cancel_application_for_order

        await cancel_application_for_order(
            session, order, reason=reject_note, commit=False
        )
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
    if (order.note or "").startswith("reseller_app:"):
        from app.services.resellers import cancel_application_for_order

        await cancel_application_for_order(
            session, order, reason=reject_note, commit=False
        )
    await session.commit()
    await session.refresh(order)
    return order


async def deliver_order(session: AsyncSession, order: Order) -> Order:
    order_id = int(order.id)
    note = _order_note(order)
    # Defense-in-depth: mutation orders must never be sealed by mint-delivery.
    # Callers should use fulfill_paid_order; this guard prevents silent no-ops
    # that look like "wrong package" / "addon not applied".
    if is_mutation_order_note(note):
        raise ValueError(
            "این سفارش تمدید/افزونه/نمایندگی است — از مسیر fulfill استفاده کنید"
        )
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
            .where(
                UserService.remark == f"order:{order_id}",
                UserService.bot_user_id == order.user_id,
            )
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
    legacy_customer_id = await optional_legacy_customer(session, order.user_id)
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
            expire = int(time.time()) + plan.duration_days * 86400

        # Reseller shop orders must always quota-check + assign ownership.
        # Creating as owner without set_owner would bypass PasarGuard max_users.
        if order.reseller_id:
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

        from app.services.pasarguard import build_user_create_payload, build_user_modify_payload, parse_group_ids

        group_ids = None
        if not plan.pg_template_id:
            group_ids = parse_group_ids(getattr(plan, "pg_group_ids", None))
            if not group_ids:
                raise ValueError(
                    "هیچ گروهی برای ساخت کاربر انتخاب نشده — در وب‌پنل برای پلن، گروه پاسارگارد را انتخاب کنید"
                )

        volume_hold = await _hold_delivery_volume(session, order, plan, pg, data_limit=data_limit, quantity=qty)
        created_pg_ids: list[int] = []
        services: list[UserService] = []
        panel = {"touched": False}

        async def _create_one(index: int) -> UserService:
            # ``{id}`` = order id (catalog); ``buyer_id`` resolves ``{username}``.
            username = await generate_pg_username(
                session,
                user_id=int(order.id),
                buyer_id=int(order.user_id),
                plan=plan,
                reseller_id=order.reseller_id,
                user=order.user,
            )
            note = f"PGClockBot order #{order.id}"
            if qty > 1:
                note = f"{note} ({index}/{qty})"
            panel["touched"] = True
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
            if plan.pg_template_id and _order_note(order) == CUSTOM_ORDER_NOTE and pg_uid:
                # A template brings its own volume and expiry; the buyer paid for the chosen ones.
                applied = await pg.modify_user_by_id(
                    int(pg_uid), build_user_modify_payload(data_limit=data_limit, expire_ts=expire)
                )
                if isinstance(applied, dict):
                    pg_user = {**pg_user, **applied}
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

            sub_url = user_subscription_url(pg_user if isinstance(pg_user, dict) else None)
            service = UserService(
                bot_user_id=order.user_id,
                customer_id=legacy_customer_id,
                plan_id=plan.id,
                pg_user_id=pg_uid,
                pg_username=pg_user.get("username", username),
                subscription_url=sub_url,
                subscription_token=extract_sub_token(sub_url),
                remark=f"order:{order.id}",
                shop_id=order.reseller_id,
            )
            from app.services.bot_user_admin import sync_service_quota_cache

            plan_days = int(getattr(plan, "duration_days", 0) or 0) or None
            if isinstance(pg_user, dict):
                sync_service_quota_cache(
                    service, pg_user, fallback_duration_days=plan_days
                )
            else:
                sync_service_quota_cache(
                    service,
                    expire_ts=expire,
                    data_limit_bytes=data_limit,
                    fallback_duration_days=plan_days,
                )
            session.add(service)
            await session.flush()
            await ensure_member(session, order.user_id, order.reseller_id)
            return service

        try:
            for i in range(1, qty + 1):
                services.append(await _create_one(i))
        except Exception as exc:
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
            # After the deletes, so this commit cannot persist services of deleted panel users.
            await settle_failed_hold(session, volume_hold, exc, panel_touched=panel["touched"])
            raise

        order.service_id = services[0].id if services else None
        order.status = OrderStatus.DELIVERED.value
        # The services now carry the volume; drop the hold in the same commit.
        await release_volume_hold(session, volume_hold)
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


MIN_TOPUP_AMOUNT = 1000
# Soft ceiling to prevent absurd pending top-ups / receipt spam.
MAX_TOPUP_AMOUNT = 500_000_000


async def create_wallet_topup(
    session: AsyncSession,
    user_id: int,
    amount: int,
    *,
    method: str = PaymentMethod.CARD.value,
    wallet_shop_id: int | None = None,
) -> Payment:
    if amount < MIN_TOPUP_AMOUNT:
        raise ValueError("حداقل مبلغ شارژ ۱۰۰۰ است")
    if amount > MAX_TOPUP_AMOUNT:
        raise ValueError("مبلغ شارژ بیش از حد مجاز است")
    from app.services.users import current_shop_reseller_id

    # Capture bot context at creation — settlement must not re-infer from
    # sticky user.reseller_id (that minted platform balance via shop keys).
    if wallet_shop_id is None:
        wallet_shop_id = current_shop_reseller_id()
    shop_id = int(wallet_shop_id) if wallet_shop_id else None
    payment = Payment(
        user_id=user_id,
        amount=amount,
        method=method,
        status=PaymentStatus.PENDING.value,
        is_wallet_topup=True,
        wallet_shop_id=shop_id,
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
    commit: bool = True,
    request_key: str | None = None,
    expected_price: int | None = None,
) -> Order:
    """Create a pending renewal order. Caller shows pay_methods (or uses pay_with_wallet)."""
    from app.services.service_renewals import validate_renewal_selection

    shop_rid = _shop_reseller_id()
    if request_key is not None:
        if not isinstance(request_key, str) or len(request_key) != 32 or any(c not in string.hexdigits for c in request_key):
            raise ValueError("شناسه درخواست تمدید نامعتبر است")
        existing = await session.scalar(select(Order).where(Order.renewal_request_key == request_key))
        if existing is not None:
            if (existing.user_id, existing.service_id, existing.plan_id, existing.reseller_id) != (user_id, service.id, plan.id, shop_rid):
                raise ValueError("درخواست تمدید نامعتبر است")
            return existing
    terms = await validate_renewal_selection(
        session, user_id=user_id, service=service, plan=plan, shop_id=shop_rid,
    )

    quote = await quote_product(session, plan, shop_id=shop_rid)
    assert_expected_price(quote, expected_price)
    order = Order(
        user_id=user_id,
        plan_id=plan.id,
        amount=quote.price,
        status=OrderStatus.PENDING.value,
        note=f"renew:{service.id}",
        service_id=service.id,
        reseller_id=shop_rid,
        renewal_snapshot=json.dumps(terms),
        renewal_request_key=request_key,
    )
    if request_key is None:
        session.add(order)
    else:
        # SQLite's legacy transaction mode needs BEGIN before a first SAVEPOINT.
        connection = await session.connection()
        if connection.dialect.name == "sqlite":
            raw = await connection.get_raw_connection()
            if not raw.driver_connection.in_transaction:
                await connection.exec_driver_sql("BEGIN")
        try:
            async with session.begin_nested():
                session.add(order)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(select(Order).where(Order.renewal_request_key == request_key))
            if existing is None:
                raise
            if (existing.user_id, existing.service_id, existing.plan_id, existing.reseller_id) != (user_id, service.id, plan.id, shop_rid):
                raise ValueError("درخواست تمدید نامعتبر است") from None
            return existing
    if commit:
        await session.commit()
        await session.refresh(order)
    else:
        await session.flush()
    return order


@dataclass(slots=True)
class _RenewalRun:
    """Mutable state of one ``apply_renewal`` attempt, shared with its failure path."""

    snapshot: dict | None
    had_saved_target: bool
    write_attempted: bool


async def _assert_renewal_matches_service(session: AsyncSession, order: Order, service: UserService) -> None:
    from app.services.service_automation import service_shop_id

    if (
        order.user_id != service.bot_user_id or order.service_id != service.id
        or await service_shop_id(session, service) != order.reseller_id
    ):
        raise ValueError("سفارش و سرویس تمدید متعلق به یک فروشگاه و مشتری نیستند")
    if not service.pg_user_id or (service.remark or "").strip() == "linked":
        raise ValueError("تمدید این سرویس ممکن نیست")


async def _claim_renewal_order(session: AsyncSession, order: Order, service: UserService) -> bool:
    """Atomically move a paid renewal to DELIVERING; False means it was already delivered."""
    from app.services.service_cancellations import lock_service_mutation

    order_id = int(order.id)
    try:
        with session.no_autoflush:
            await lock_service_mutation(session, service.id)
            claim = await session.execute(
                update(Order)
                .where(Order.id == order_id, Order.status == OrderStatus.PAID.value)
                .values(status=OrderStatus.DELIVERING.value, service_mutation_pending=True)
                .execution_options(synchronize_session=False)
            )
    except IntegrityError as exc:
        await session.rollback()
        await session.refresh(order)
        raise ValueError("تغییر دیگری برای این سرویس در حال انجام یا بررسی است") from exc
    if claim.rowcount != 1:
        await session.refresh(order)
        if order.status == OrderStatus.DELIVERED.value:
            return False
        raise ValueError("سفارش تمدید قابل پردازش نیست")
    await session.commit()
    await session.refresh(order)
    return True


async def _release_renewal_claim(session: AsyncSession, order: Order, run: _RenewalRun) -> None:
    """Return a DELIVERING renewal to PAID; keep the quota lock once a panel write may have happened."""
    await session.execute(
        update(Order)
        .where(Order.id == int(order.id), Order.status == OrderStatus.DELIVERING.value)
        .values(status=OrderStatus.PAID.value, service_mutation_pending=bool(run.snapshot and run.write_attempted))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(order)


async def _renewal_panel_client(session: AsyncSession, order: Order, service: UserService):
    if order.reseller_id:
        from app.services.pasarguard import get_pg_for_reseller

        pg = await get_pg_for_reseller(session, int(order.reseller_id))
    else:
        pg = get_pg()
    if not service.pg_user_id:
        raise ValueError("service has no panel user")
    return pg


async def _renewal_targets(pg, service: UserService, plan: Plan, snapshot: dict | None):
    """Return ``(payload, data_limit, expire, gate_expire)``.

    ``payload`` is the absolute panel target for snapshot renewals (``None`` for
    legacy orders); ``gate_expire`` is the expiry used for the reseller
    provisioning gate, which counts an on-hold duration from now.
    """
    if snapshot is None:
        data_limit = int(plan.data_limit_gb * (1024**3)) if plan.data_limit_gb is not None else None
        expire = int(time.time()) + plan.duration_days * 86400 if plan.duration_days else None
        return None, data_limit, expire, expire
    if snapshot.get("version") != 1:
        raise ValueError("اطلاعات سفارش تمدید نامعتبر است")
    payload = snapshot.get("target")
    if payload is None:
        from app.services.service_renewals import calculate_renewal

        payload = calculate_renewal(await pg.get_user_by_id(service.pg_user_id), snapshot)["payload"]
    gate_expire = payload["expire"]
    if payload.get("on_hold_expire_duration"):
        gate_expire = int(time.time()) + payload["on_hold_expire_duration"]
    return payload, payload["data_limit"], payload["expire"], gate_expire


async def _assert_renewal_provision_gate(
    session: AsyncSession, order: Order, plan: Plan, run: _RenewalRun,
    *, data_limit: int | None, gate_expire: int | None,
) -> None:
    if not order.reseller_id:
        return
    pg_owner, pg_role_id = await _reseller_pg_link(session, order.reseller_id)
    try:
        await assert_provision_renew(
            session,
            reseller_user_id=int(order.reseller_id),
            pg_admin_username=pg_owner,
            pg_role_id=pg_role_id,
            data_limit=data_limit,
            expire_ts=gate_expire,
            from_template=bool(plan.pg_template_id) if run.snapshot is None else False,
        )
    except ProvisionError as e:
        raise ValueError(e.message) from e


async def _started_on_hold_renewal(pg, service: UserService, snapshot: dict, payload: dict) -> dict | None:
    """Return the live panel user when an earlier attempt already applied the on-hold target.

    ``None`` means the user is still on hold, so the target must be written.
    Anything that does not match the saved target needs operator review.
    """
    from app.services.formatting import hold_duration_from_info, is_on_hold_status, parse_expire
    from app.services.service_live_info import validate_service_info
    from app.services.service_renewals import RenewalPendingReview

    live = validate_service_info(await pg.get_user_by_id(service.pg_user_id))
    if live.get("error"):
        raise RenewalPendingReview()
    if is_on_hold_status(live["status"]):
        return None
    live_expire = parse_expire(live.get("expire", live.get("expire_date")))
    duration = payload["on_hold_expire_duration"]
    started_expire = int(live_expire.timestamp()) if live_expire else 0
    matches_target = (
        live["status"] == "active"
        and int(live["data_limit"] or 0) == payload["data_limit"]
        and hold_duration_from_info(live) == duration
        and snapshot["prepared_at"] + duration - 1 <= started_expire <= int(time.time()) + duration + 1
    )
    if not matches_target:
        raise RenewalPendingReview()
    return live


async def _write_renewal_target(
    session: AsyncSession, order: Order, pg, service: UserService, run: _RenewalRun, payload: dict,
) -> dict:
    """Freeze the absolute target on the order, then write it to the panel."""
    snapshot = run.snapshot
    snapshot["target"] = payload
    if not run.had_saved_target:
        snapshot["prepared_at"] = int(time.time())
    order.renewal_snapshot = json.dumps(snapshot)
    await session.commit()
    # From here a retry must reuse the saved target instead of recomputing it.
    run.write_attempted = True
    pg_user = None
    if run.had_saved_target and payload["status"] == "on_hold":
        pg_user = await _started_on_hold_renewal(pg, service, snapshot, payload)
    if pg_user is None:
        pg_user = await pg.modify_user_by_id(service.pg_user_id, payload)
    return {**payload, **(pg_user if isinstance(pg_user, dict) else {})}


async def _write_legacy_renewal(pg, service: UserService, plan: Plan, *, data_limit: int | None, expire: int | None):
    if plan.pg_template_id:
        return await pg.modify_user_with_template(service.pg_user_id, {"user_template_id": plan.pg_template_id})
    return await pg.modify_user_by_id(
        service.pg_user_id, {"status": "active", "data_limit": data_limit, "expire": expire},
    )


def _record_renewed_service(
    service: UserService, plan: Plan, pg_user, run: _RenewalRun, *, data_limit: int | None, expire: int | None,
) -> None:
    from app.services.bot_user_admin import sync_service_quota_cache

    sub_url = user_subscription_url(pg_user if isinstance(pg_user, dict) else None) or service.subscription_url
    service.subscription_url = sub_url
    service.subscription_token = extract_sub_token(sub_url)
    service.plan_id = plan.id
    service.notified_expire = False
    service.notified_traffic = False
    seconds = run.snapshot["duration_seconds"] if run.snapshot else int(getattr(plan, "duration_days", 0) or 0) * 86400
    plan_days = seconds // 86400 or None
    if isinstance(pg_user, dict):
        sync_service_quota_cache(service, pg_user, fallback_duration_days=plan_days)
    else:
        sync_service_quota_cache(
            service, expire_ts=expire, data_limit_bytes=data_limit, fallback_duration_days=plan_days,
        )


async def _notify_renewal_delivered(session: AsyncSession, order: Order) -> None:
    try:
        from app.services.loyalty import consume_loyalty_discount_for_order, on_order_delivered

        await consume_loyalty_discount_for_order(session, order)
        await session.commit()
        await on_order_delivered(session, order)
    except Exception:
        logger.warning("loyalty on_order_delivered renew failed order=%s", order.id, exc_info=True)


async def _release_failed_renewal(session: AsyncSession, order: Order, run: _RenewalRun, exc: Exception) -> bool:
    """Undo the claim after a failed attempt; True when the panel outcome is unknown."""
    from app.services.pasarguard import PasarGuardError

    # A rejected request has a known outcome; transport and DB failures do not.
    rejected = isinstance(exc, PasarGuardError) and exc.status_code in (400, 401, 403, 404, 422)
    if rejected and not run.had_saved_target:
        run.write_attempted = False
        if run.snapshot is not None:
            run.snapshot.pop("target", None)
            order.renewal_snapshot = json.dumps(run.snapshot)
    try:
        if not session.is_active:
            await session.rollback()
        await _release_renewal_claim(session, order, run)
    except Exception:
        logger.warning("renewal claim release failed order=%s", order.id, exc_info=True)
    return run.snapshot is not None and run.write_attempted


async def _hold_renewal_volume(
    session: AsyncSession, order: Order, service: UserService, plan: Plan, pg, run: _RenewalRun,
    *, data_limit: int | None,
) -> HeldVolume | None:
    """Hold the volume a renewal adds on top of the service's current limit."""
    if not order.reseller_id:
        return None
    new_limit = data_limit
    if run.snapshot is None and plan.pg_template_id:
        new_limit = await template_limit_if_gated(session, int(order.reseller_id), pg, int(plan.pg_template_id))
    elif new_limit is None:
        new_limit = 0
    try:
        return await hold_limit_change(
            session, shop_id=int(order.reseller_id), current_limit=service.quota_data_limit_bytes,
            new_limit=new_limit, key=f"renew:{int(order.id)}", audience="customer",
        )
    except ProvisionError as e:
        raise ValueError(e.message) from e


async def apply_renewal(
    session: AsyncSession, order: Order, service: UserService, plan: Plan,
    *, reset_traffic: bool = False,
) -> Order:
    from app.services.service_renewals import RenewalPendingReview

    snapshot_raw = getattr(order, "renewal_snapshot", None)
    snapshot = json.loads(snapshot_raw) if snapshot_raw else None
    if snapshot is not None:
        await _assert_renewal_matches_service(session, order, service)
    if not await _claim_renewal_order(session, order, service):
        return order

    had_saved_target = bool(snapshot and snapshot.get("target"))
    run = _RenewalRun(snapshot=snapshot, had_saved_target=had_saved_target, write_attempted=had_saved_target)
    volume_hold: HeldVolume | None = None
    panel_touched = had_saved_target
    try:
        pg = await _renewal_panel_client(session, order, service)
        payload, data_limit, expire, gate_expire = await _renewal_targets(pg, service, plan, snapshot)
        await _assert_renewal_provision_gate(session, order, plan, run, data_limit=data_limit, gate_expire=gate_expire)
        volume_hold = await _hold_renewal_volume(session, order, service, plan, pg, run, data_limit=data_limit)
        panel_touched = True
        if snapshot is not None:
            pg_user = await _write_renewal_target(session, order, pg, service, run, payload)
        else:
            pg_user = await _write_legacy_renewal(pg, service, plan, data_limit=data_limit, expire=expire)
            if reset_traffic:
                await pg.reset_user_by_id(service.pg_user_id)
        _record_renewed_service(service, plan, pg_user, run, data_limit=data_limit, expire=expire)
        order.status = OrderStatus.DELIVERED.value
        order.service_mutation_pending = False
        await release_volume_hold(session, volume_hold)
        await session.commit()
        await session.refresh(order)
        await _notify_renewal_delivered(session, order)
        return order
    except Exception as exc:
        pending_review = await _release_failed_renewal(session, order, run, exc)
        await settle_failed_hold(session, volume_hold, exc, panel_touched=panel_touched)
        if pending_review:
            raise RenewalPendingReview() from exc
        raise
