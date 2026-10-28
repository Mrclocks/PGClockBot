"""Customer service addon packs (extra GB / days) — shop-scoped, fail-closed."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.catalog_pricing import annotate_prices, assert_expected_price, quote_product
from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Plan,
    ServiceAddonPack,
    UserService,
)
from app.services.service_addon_targets import calculate_addon_target
from app.services.plans_catalog import require_catalog_owner_id
from app.services.provision_gate import ProvisionError, hold_limit_change, new_hold_key
from app.services.volume_allocation import (
    HeldVolume,
    VolumeAllocationError,
    assert_volume_delta_allowed,
    is_hard_panel_rejection,
    release_volume_hold,
)
from app.services.shop_scope import ShopScopeError, is_platform_admin, shop_owner_id

log = logging.getLogger(__name__)

KIND_VOLUME = "volume"
KIND_DURATION = "duration"
VALID_KINDS = frozenset({KIND_VOLUME, KIND_DURATION})

# note: svc_addon:{pack_id}:{service_id}[:{kind}:{amount}]
# kind/amount snapshot freezes entitlement at order time (price is on order.amount).
_NOTE_RE = re.compile(
    r"^svc_addon:(\d+):(\d+)(?::(volume|duration):([0-9]+(?:\.[0-9]+)?))?$"
)

MAX_VOLUME_GB = 10_000.0
MAX_DURATION_DAYS = 3650
MAX_PRICE = 2_000_000_000


def format_addon_note(
    pack_id: int,
    service_id: int,
    *,
    kind: str,
    amount: float,
) -> str:
    """Server-written order note with snapshotted kind/amount."""
    k = (kind or "").strip().lower()
    if k not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    amt = float(amount)
    if k == KIND_DURATION:
        amt_s = str(int(amt))
    else:
        # Avoid :g scientific notation (e.g. 1e+06) which breaks _NOTE_RE.
        if amt == int(amt) and abs(amt) < 1e15:
            amt_s = str(int(amt))
        else:
            amt_s = format(amt, ".10f").rstrip("0").rstrip(".")
            if not amt_s or amt_s == "-":
                amt_s = "0"
    return f"svc_addon:{int(pack_id)}:{int(service_id)}:{k}:{amt_s}"


def parse_addon_note(
    note: str | None,
) -> tuple[int, int, str | None, float | None] | None:
    """Return (pack_id, service_id, kind|None, amount|None). Legacy notes omit snapshot."""
    m = _NOTE_RE.match((note or "").strip())
    if not m:
        return None
    pack_id = int(m.group(1))
    service_id = int(m.group(2))
    kind = m.group(3)
    amt_raw = m.group(4)
    if kind and amt_raw is not None:
        return pack_id, service_id, kind, float(amt_raw)
    return pack_id, service_id, None, None


def apply_pack_owner_filter(query, staff: dict | None):
    if is_platform_admin(staff):
        return query.where(ServiceAddonPack.owner_reseller_id.is_(None))
    if not staff:
        return query.where(ServiceAddonPack.owner_reseller_id.is_(None))
    rid = shop_owner_id(staff)
    if not rid:
        return query.where(ServiceAddonPack.id < 0)
    return query.where(ServiceAddonPack.owner_reseller_id == rid)


def pack_belongs_to_staff(pack: ServiceAddonPack | None, staff: dict | None) -> bool:
    if not pack:
        return False
    if is_platform_admin(staff):
        return pack.owner_reseller_id is None
    if not staff:
        return pack.owner_reseller_id is None
    rid = shop_owner_id(staff)
    if not rid:
        return False
    return int(pack.owner_reseller_id or 0) == int(rid)


def pack_matches_shop(pack: ServiceAddonPack | None, shop_rid: int | None) -> bool:
    if not pack or not pack.is_active:
        return False
    if shop_rid:
        return int(pack.owner_reseller_id or 0) == int(shop_rid)
    return pack.owner_reseller_id is None


def category_ids_equal(a: int | None, b: int | None) -> bool:
    """Strict category match — both unset, or the same positive id."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return int(a) == int(b)


def pack_matches_service_category(
    pack: ServiceAddonPack | None, service_category_id: int | None
) -> bool:
    """A general pack fits every category of its shop; any other pack only its own.

    Cross-category purchase of a category pack stays fail-closed, and a pack that is
    neither general nor tied to a category (its category was removed) matches nothing.
    """
    if not pack:
        return False
    if pack.is_general:
        return True
    if pack.category_id is None:
        return False
    return category_ids_equal(
        int(pack.category_id),
        int(service_category_id) if service_category_id is not None else None,
    )


def prefer_category_packs(packs: list[ServiceAddonPack]) -> list[ServiceAddonPack]:
    """Drop a general pack when a category pack offers the same kind and amount.

    The category pack is the shop's price for that category, so customers never see
    two prices for the same bundle. The input order is kept.
    """
    specific = {(pack.kind, float(pack.amount)) for pack in packs if not pack.is_general}
    return [pack for pack in packs if not pack.is_general or (pack.kind, float(pack.amount)) not in specific]


async def service_plan_category_id(
    session: AsyncSession, service: UserService
) -> int | None:
    """Category of the sales plan linked to this service (None if unset/missing)."""
    if not service or not service.plan_id:
        return None
    plan = await session.get(Plan, int(service.plan_id))
    if not plan or plan.category_id is None:
        return None
    return int(plan.category_id)


def is_addon_order(order: Order | None) -> bool:
    return bool(order and parse_addon_note(order.note))


def kind_label(kind: str) -> str:
    return "حجم" if kind == KIND_VOLUME else "زمان"


def format_amount_label(kind: str, amount: float) -> str:
    if kind == KIND_VOLUME:
        amt = float(amount)
        if amt == int(amt):
            return f"{int(amt)} گیگ"
        return f"{amt:g} گیگ"
    days = int(float(amount))
    return f"{days} روز"


def amount_label(pack: ServiceAddonPack) -> str:
    return format_amount_label(pack.kind, float(pack.amount))


async def list_packs(
    session: AsyncSession,
    staff: dict | None,
    *,
    active_only: bool = False,
    kind: str | None = None,
) -> list[ServiceAddonPack]:
    q = select(ServiceAddonPack).order_by(
        ServiceAddonPack.kind, ServiceAddonPack.sort_order, ServiceAddonPack.id
    )
    q = apply_pack_owner_filter(q, staff)
    if active_only:
        q = q.where(ServiceAddonPack.is_active.is_(True))
    if kind:
        if kind not in VALID_KINDS:
            raise ValueError("نوع بسته نامعتبر است")
        q = q.where(ServiceAddonPack.kind == kind)
    return list((await session.execute(q)).scalars().all())


async def list_shop_packs(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
    active_only: bool = True,
    kind: str | None = None,
    service_category_id: int | None | object = ...,
) -> list[ServiceAddonPack]:
    """List shop packs. Pass ``service_category_id`` to get what a service of that plan
    category may buy: the general packs plus the packs tied to that category (``None``
    means a service without a category, which only sees general packs).

    Omit it (default) to return every shop pack (admin lists).
    """
    from app.services.users import current_shop_reseller_id

    rid = reseller_id if reseller_id is not None else current_shop_reseller_id()
    q = select(ServiceAddonPack).order_by(
        ServiceAddonPack.kind, ServiceAddonPack.sort_order, ServiceAddonPack.id
    )
    if rid:
        q = q.where(ServiceAddonPack.owner_reseller_id == int(rid))
    else:
        q = q.where(ServiceAddonPack.owner_reseller_id.is_(None))
    if active_only:
        q = q.where(ServiceAddonPack.is_active.is_(True))
    if kind:
        q = q.where(ServiceAddonPack.kind == kind)
    if service_category_id is not ...:
        fits = ServiceAddonPack.is_general.is_(True)
        if service_category_id is not None:
            fits = or_(fits, ServiceAddonPack.category_id == int(service_category_id))
        q = q.where(fits)
    packs = list((await session.execute(q)).scalars().all())
    if service_category_id is not ...:
        packs = prefer_category_packs(packs)
    await annotate_prices(session, packs, shop_id=rid)
    return packs


async def list_packs_for_service(
    session: AsyncSession,
    service: UserService,
    *,
    kind: str | None = None,
    active_only: bool = True,
) -> list[ServiceAddonPack]:
    """Packs purchasable for one service — shop scope ∧ matching plan category."""
    from app.services.service_automation import service_shop_id

    shop_rid = await service_shop_id(session, service)
    cat_id = await service_plan_category_id(session, service)
    return await list_shop_packs(
        session,
        reseller_id=shop_rid,
        active_only=active_only,
        kind=kind,
        service_category_id=cat_id,
    )


async def get_owned_pack(
    session: AsyncSession, pack_id: int, staff: dict | None
) -> ServiceAddonPack | None:
    pack = await session.get(ServiceAddonPack, int(pack_id))
    if not pack_belongs_to_staff(pack, staff):
        return None
    return pack


def _validate_pack_fields(
    *,
    name: str,
    kind: str,
    amount: float,
    price: int,
    description: str | None,
) -> tuple[str, str, float, int, str | None]:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("نام بسته الزامی است")
    if len(cleaned) > 128:
        raise ValueError("نام بسته خیلی طولانی است")
    k = (kind or "").strip().lower()
    if k not in VALID_KINDS:
        raise ValueError("نوع بسته باید حجم یا زمان باشد")
    try:
        amt = float(amount)
    except (TypeError, ValueError) as e:
        raise ValueError("مقدار بسته نامعتبر است") from e
    if amt <= 0:
        raise ValueError("مقدار باید بزرگ‌تر از صفر باشد")
    if k == KIND_VOLUME and amt > MAX_VOLUME_GB:
        raise ValueError("حجم بیش از حد مجاز است")
    if k == KIND_DURATION:
        if amt != int(amt):
            raise ValueError("روز باید عدد صحیح باشد")
        if amt > MAX_DURATION_DAYS:
            raise ValueError("مدت بیش از حد مجاز است")
        amt = float(int(amt))
    try:
        price_i = int(price)
    except (TypeError, ValueError) as e:
        raise ValueError("قیمت نامعتبر است") from e
    if price_i < 0 or price_i > MAX_PRICE:
        raise ValueError("قیمت نامعتبر است")
    desc = (description or "").strip() or None
    if desc and len(desc) > 255:
        raise ValueError("توضیح خیلی طولانی است")
    return cleaned, k, amt, price_i, desc


async def create_pack(
    session: AsyncSession,
    staff: dict | None,
    *,
    name: str,
    kind: str,
    amount: float,
    price: int,
    description: str | None = None,
    sort_order: int = 0,
    category_id: int | None | object = ...,
) -> ServiceAddonPack:
    from app.services.plan_categories import (
        AUDIENCE_USERS,
        resolve_category_for_plan_write,
    )

    owner_id = require_catalog_owner_id(staff)
    cleaned, k, amt, price_i, desc = _validate_pack_fields(
        name=name, kind=kind, amount=amount, price=price, description=description
    )
    resolved_cat: int | None = None
    if category_id is not ...:
        resolved_cat = await resolve_category_for_plan_write(
            session,
            staff,
            category_id,
            expected_audience=AUDIENCE_USERS,
        )
    pack = ServiceAddonPack(
        name=cleaned,
        kind=k,
        amount=amt,
        price=price_i,
        description=desc,
        owner_reseller_id=owner_id,
        category_id=resolved_cat,
        is_general=resolved_cat is None,
        sort_order=int(sort_order or 0),
        is_active=True,
    )
    session.add(pack)
    await session.commit()
    await session.refresh(pack)
    return pack


async def update_pack(
    session: AsyncSession,
    staff: dict | None,
    pack_id: int,
    *,
    name: str | None = None,
    kind: str | None = None,
    amount: float | None = None,
    price: int | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
    category_id: int | None | object = ...,
) -> ServiceAddonPack:
    from app.services.plan_categories import (
        AUDIENCE_USERS,
        resolve_category_for_plan_write,
    )

    pack = await get_owned_pack(session, pack_id, staff)
    if not pack:
        raise ShopScopeError("بسته یافت نشد")
    cleaned, k, amt, price_i, desc = _validate_pack_fields(
        name=name if name is not None else pack.name,
        kind=kind if kind is not None else pack.kind,
        amount=amount if amount is not None else pack.amount,
        price=price if price is not None else pack.price,
        description=description if description is not None else (pack.description or ""),
    )
    pack.name = cleaned
    pack.kind = k
    pack.amount = amt
    pack.price = price_i
    if description is not None:
        pack.description = desc
    if sort_order is not None:
        pack.sort_order = int(sort_order)
    if is_active is not None:
        pack.is_active = bool(is_active)
    if category_id is not ...:
        pack.category_id = await resolve_category_for_plan_write(
            session,
            staff,
            category_id,
            expected_audience=AUDIENCE_USERS,
            allow_inactive_id=pack.category_id,
        )
        pack.is_general = pack.category_id is None
    await session.commit()
    await session.refresh(pack)
    return pack


async def delete_pack(
    session: AsyncSession, staff: dict | None, pack_id: int
) -> None:
    pack = await get_owned_pack(session, pack_id, staff)
    if not pack:
        raise ShopScopeError("بسته یافت نشد")
    await session.delete(pack)
    await session.commit()


def pack_volume_bytes(amount: float) -> int:
    """Bytes a volume pack adds, with the rounding the panel write uses."""
    return int(float(amount) * (1024**3))


async def _assert_pack_volume_available(
    session: AsyncSession, shop_rid: int | None, pack: ServiceAddonPack
) -> None:
    """Refuse a volume pack the shop cannot apply within its ceiling; delivery holds it again."""
    try:
        await assert_volume_delta_allowed(
            session, shop_id=shop_rid, delta_bytes=pack_volume_bytes(pack.amount), audience="customer",
        )
    except VolumeAllocationError as e:
        raise ValueError(e.user_message()) from e


async def create_addon_order(
    session: AsyncSession,
    *,
    user_id: int,
    service: UserService,
    pack: ServiceAddonPack,
    commit: bool = True,
    expected_price: int | None = None,
) -> Order:
    """Create a pending addon order. Price is snapshotted from the pack."""
    from app.services.orders import _shop_reseller_id

    if not pack or not pack.is_active:
        raise ValueError("بسته یافت نشد")
    if pack.kind not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    shop_rid = _shop_reseller_id()
    if not pack_matches_shop(pack, shop_rid):
        raise ValueError("این بسته در این فروشگاه موجود نیست")
    if service.integration_id is not None or service.bot_user_id != user_id:
        raise ValueError("سرویس متعلق به شما نیست")
    if not service.pg_user_id:
        raise ValueError("سرویس به پنل متصل نیست")
    if (service.remark or "").strip() == "linked":
        raise ValueError("سرویس متصل‌شده فقط مشاهده است؛ خرید افزونه ممکن نیست")

    # Fail closed: service owner must live in the same shop as the pack.
    user = await session.get(BotUser, int(user_id))
    if not user:
        raise ValueError("کاربر یافت نشد")
    from app.services.service_automation import service_shop_id

    if await service_shop_id(session, service) != shop_rid:
        raise ValueError("سرویس این فروشگاه نیست")

    # Fail closed: pack category must match the service's sales-plan category.
    svc_cat = await service_plan_category_id(session, service)
    if not pack_matches_service_category(pack, svc_cat):
        raise ValueError(
            "این بسته برای دستهٔ پلن این سرویس نیست — بسته متناسب با همان سرویس را انتخاب کنید"
        )

    from app.services.service_renewals import assert_service_quota_available

    await assert_service_quota_available(session, service.id)

    # Fail closed on known-unlimited quotas (synced cache) so buyers are not charged
    # for an entitlement that cannot be applied.
    if service.quota_synced_at is not None:
        if pack.kind == KIND_VOLUME and int(service.quota_data_limit_bytes or 0) <= 0:
            raise ValueError("این سرویس حجم نامحدود دارد — بسته حجم قابل خرید نیست")
        if pack.kind == KIND_DURATION and service.quota_expire_at is None:
            raise ValueError("این سرویس زمان نامحدود دارد — بسته زمان قابل خرید نیست")

    if pack.kind == KIND_VOLUME:
        await _assert_pack_volume_available(session, shop_rid, pack)

    quote = await quote_product(session, pack, shop_id=shop_rid)
    assert_expected_price(quote, expected_price)
    order = Order(
        user_id=user_id,
        plan_id=None,
        amount=quote.price,
        status=OrderStatus.PENDING.value,
        note=format_addon_note(
            int(pack.id),
            int(service.id),
            kind=pack.kind,
            amount=float(pack.amount),
        ),
        service_id=int(service.id),
        reseller_id=shop_rid,
    )
    session.add(order)
    if commit:
        await session.commit()
        await session.refresh(order)
    else:
        await session.flush()
    return order


async def apply_service_addon(
    session: AsyncSession, order: Order
) -> Order:
    """Apply paid addon to the linked UserService (additive volume/time)."""
    parsed = parse_addon_note(order.note)
    if not parsed:
        raise ValueError("سفارش افزونه نامعتبر است")
    pack_id, service_id, snap_kind, snap_amount = parsed
    order_id = int(order.id)

    try:
        with session.no_autoflush:
            from app.services.service_cancellations import lock_service_mutation
            await lock_service_mutation(session, service_id)
            claim = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.PAID.value,
                )
                .values(status=OrderStatus.DELIVERING.value, service_mutation_pending=True)
                .execution_options(synchronize_session=False)
            )
    except IntegrityError as exc:
        await session.rollback()
        await session.refresh(order)
        await session.get(BotUser, order.user_id)
        raise ValueError("تغییر دیگری برای این سرویس در حال انجام یا بررسی است") from exc
    if claim.rowcount != 1:
        await session.refresh(order)
        if order.status == OrderStatus.DELIVERED.value:
            return order
        raise ValueError("سفارش افزونه قابل پردازش نیست")
    await session.commit()
    await session.refresh(order)

    async def _release() -> None:
        await session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.status == OrderStatus.DELIVERING.value,
            )
            .values(status=OrderStatus.PAID.value, service_mutation_pending=False)
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    pg_applied = False
    try:
        pack = await session.get(ServiceAddonPack, pack_id)
        service = await session.get(UserService, service_id)
        if not pack or not service:
            raise ValueError("بسته یا سرویس یافت نشد")
        if int(order.service_id or 0) != int(service.id):
            raise ValueError("سرویس سفارش با یادداشت همخوان نیست")
        if service.integration_id is not None or service.bot_user_id != order.user_id:
            raise ValueError("سرویس متعلق به خریدار نیست")
        # Shop isolation: pack + order must match
        if order.reseller_id:
            if int(pack.owner_reseller_id or 0) != int(order.reseller_id):
                raise ValueError("بسته متعلق به این فروشگاه نیست")
        elif pack.owner_reseller_id is not None:
            raise ValueError("بسته متعلق به این فروشگاه نیست")

        # Prefer snapshotted kind/amount from the order note; fall back to live pack
        # only for legacy notes written before snapshotting.
        apply_kind = snap_kind or pack.kind
        apply_amount = float(snap_amount) if snap_amount is not None else float(pack.amount)
        if apply_kind not in VALID_KINDS:
            raise ValueError("نوع بسته نامعتبر است")

        # PG mutate first. After it succeeds we must not release the claim —
        # releasing would let a retry double-apply capacity.
        await _apply_pack_to_service(
            session,
            service,
            kind=apply_kind,
            amount=apply_amount,
            order_reseller_id=order.reseller_id,
            order_id=int(order_id),
        )
        pg_applied = True

        with session.no_autoflush:
            done = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.DELIVERING.value,
                )
                .values(status=OrderStatus.DELIVERED.value, service_mutation_pending=False)
                .execution_options(synchronize_session=False)
            )
        if done.rowcount != 1:
            # PG already mutated — do not release; seal below in except path.
            log.error(
                "addon deliver seal failed after PG apply order=%s", order_id
            )
            raise ValueError("ثبت تحویل افزونه ناموفق بود")
        from app.services.loyalty import consume_loyalty_discount_for_order

        await consume_loyalty_discount_for_order(session, order)
        await session.commit()
        await session.refresh(order)
        return order
    except Exception:
        if not pg_applied:
            try:
                await _release()
            except Exception:
                log.exception("addon claim release failed order=%s", order_id)
        else:
            # Capacity already on PG — leave order DELIVERED so retries do not
            # re-apply. Prefer seal over leaving DELIVERING/PAID.
            try:
                await session.execute(
                    update(Order)
                    .where(
                        Order.id == order_id,
                        Order.status == OrderStatus.DELIVERING.value,
                    )
                    .values(status=OrderStatus.DELIVERED.value, service_mutation_pending=False)
                    .execution_options(synchronize_session=False)
                )
                from app.services.loyalty import consume_loyalty_discount_for_order

                await consume_loyalty_discount_for_order(session, order)
                await session.commit()
            except Exception:
                log.exception(
                    "addon post-PG seal failed order=%s — manual check needed",
                    order_id,
                )
        raise


async def _on_hold_activation_expire_ts(
    session: AsyncSession,
    service: UserService,
    info: dict[str, Any],
    *,
    now: int,
) -> int:
    """Resolve a finite expiration before activating a pending-start user."""
    from app.services.formatting import on_hold_expire_duration_seconds, parse_expire

    current = parse_expire(info.get("expire") or info.get("expire_date"))
    if current is not None:
        return int(current.timestamp())
    duration = on_hold_expire_duration_seconds(info)
    if duration is not None:
        return now + duration

    # apply_service_addon loads UserService via session.get, so plan may be
    # unloaded. Never trigger an async relationship load through getattr.
    plan = service.__dict__.get("plan")
    if plan is None and service.plan_id:
        plan = await session.get(Plan, int(service.plan_id))
    plan_days = int(getattr(plan, "duration_days", 0) or 0) if plan else 0
    if plan_days <= 0:
        raise ValueError("مدت سرویس در انتظار مشخص نیست — بسته قابل اعمال نیست")
    return now + plan_days * 86400


async def _hold_pack_volume(
    session: AsyncSession, service: UserService, *, shop_id: int | None, new_limit: int | None, key: str
) -> HeldVolume | None:
    """Hold a volume pack's increase in the shop's ceiling before the panel write."""
    if new_limit is None or not shop_id:
        return None
    try:
        return await hold_limit_change(
            session, shop_id=int(shop_id), current_limit=service.quota_data_limit_bytes,
            new_limit=int(new_limit), key=key, audience="customer",
        )
    except ProvisionError as e:
        raise ValueError(e.message) from e


async def _write_pack(
    session: AsyncSession, pg: Any, service: UserService, payload: dict[str, Any], hold: HeldVolume | None
) -> None:
    """Write the pack to the panel; the hold is dropped in the caller's commit of the new limit."""
    try:
        await pg.modify_user_by_id(int(service.pg_user_id), payload)
    except Exception as exc:
        # Refused by the panel: nothing was granted. Any other failure keeps the hold until it expires.
        if is_hard_panel_rejection(exc):
            await release_volume_hold(session, hold)
        raise
    await release_volume_hold(session, hold)


async def _apply_pack_to_service(
    session: AsyncSession,
    service: UserService,
    *,
    kind: str,
    amount: float,
    order_reseller_id: int | None,
    order_id: int | None = None,
) -> None:
    """Additive PG mutate — never falls back across shop PG credentials.

    Unlimited volume/time services are rejected (not converted to limited).
    """
    from app.services.bot_user_admin import sync_service_quota_cache
    from app.services.formatting import is_on_hold_status
    from app.services.pasarguard import get_pg, get_pg_for_reseller

    if not service.pg_user_id:
        raise ValueError("سرویس به پنل متصل نیست")

    if order_reseller_id:
        pg = await get_pg_for_reseller(session, int(order_reseller_id))
    else:
        from app.services.users import current_shop_reseller_id

        if current_shop_reseller_id():
            raise ValueError("فروشگاه بدون اعتبار پاسارگارد معتبر است")
        pg = get_pg()

    info = await pg.get_user_by_id(int(service.pg_user_id))
    if not isinstance(info, dict):
        raise ValueError("کاربر پاسارگارد یافت نشد")

    now = int(time.time())
    on_hold_expire = None
    if is_on_hold_status(info.get("status")):
        on_hold_expire = await _on_hold_activation_expire_ts(session, service, info, now=now)
    payload = calculate_addon_target(info, kind=kind,
        amount_bytes=pack_volume_bytes(amount) if kind == KIND_VOLUME else None,
        amount_seconds=int(float(amount))*86400 if kind == KIND_DURATION else None,
        now=now, on_hold_expire_ts=on_hold_expire)
    expire_ts = payload.get("expire")
    data_limit_bytes = payload.get("data_limit")

    hold = await _hold_pack_volume(
        session, service, shop_id=order_reseller_id,
        new_limit=data_limit_bytes if kind == KIND_VOLUME else None, key=f"addon:{int(order_id)}" if order_id else new_hold_key("addon"),
    )
    await _write_pack(session, pg, service, payload, hold)
    # Cache sync is best-effort — PG already mutated. Never fail the delivery
    # claim here or a retry would double-apply the same addon.
    try:
        sync_service_quota_cache(
            service,
            expire_ts=expire_ts,
            data_limit_bytes=data_limit_bytes,
        )
    except Exception:
        log.exception(
            "addon quota cache sync failed svc=%s kind=%s",
            getattr(service, "id", None),
            kind,
        )
