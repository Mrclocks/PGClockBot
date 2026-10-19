"""Validate shop price adjustments and atomically enqueue their announcements."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.db.models import (
    BotUser, CampaignRecipient, CatalogDiscount, MarketingPreference, Order,
    Plan, PlanCategory, TargetedCampaign,
)
from app.services.authz import authz_from_staff, can_shop
from app.services.catalog_pricing import MAX_ACTIVE_DISCOUNTS, MAX_PRICE
from app.services.formatting import format_number, format_toman
from app.services.gift_codes import expiry_input
from app.services.shop_scope import resolve_shop_scope_id

SCOPES = {"plan": "یک سرویس / پلن", "category": "یک دسته‌بندی", "fixed": "همهٔ پلن‌های ثابت", "all": "همهٔ بخش‌های فروشگاه"}
MODES = {"temporary": "تخفیف زمان‌دار", "permanent": "کاهش دائمی قیمت پایه"}


@dataclass(frozen=True, slots=True)
class DiscountInput:
    title: str
    scope: str
    mode: str
    reduction_kind: str
    reduction_value: int
    duration_hours: int | None = None
    plan_id: int | None = None
    category_id: int | None = None
    notify: bool = True
    request_key: str = ""


def discount_scope(staff: dict) -> int | None:
    if not can_shop(authz_from_staff(staff), "catalog_discounts"):
        raise PermissionError("دسترسی مدیریت تخفیف ندارید")
    return resolve_shop_scope_id(staff)


def validate_input(data: DiscountInput) -> None:
    if not isinstance(data.title, str) or not 1 <= len(data.title.strip()) <= 128:
        raise ValueError("عنوان باید بین ۱ تا ۱۲۸ نویسه باشد")
    if data.scope not in SCOPES or data.mode not in MODES or data.reduction_kind not in {"percent", "amount"}:
        raise ValueError("نوع یا محدودهٔ تخفیف نامعتبر است")
    ceiling = 100 if data.reduction_kind == "percent" else MAX_PRICE
    if type(data.reduction_value) is not int or not 1 <= data.reduction_value <= ceiling:
        raise ValueError("مقدار کاهش قیمت نامعتبر است")
    if data.mode == "temporary" and (type(data.duration_hours) is not int or not 1 <= data.duration_hours <= 8760):
        raise ValueError("مدت تخفیف باید بین ۱ تا ۸۷۶۰ ساعت باشد")
    if data.mode == "permanent" and data.duration_hours is not None:
        raise ValueError("کاهش دائمی تاریخ پایان ندارد")
    for value in (data.plan_id, data.category_id):
        if value is not None and (type(value) is not int or not 1 <= value <= MAX_PRICE):
            raise ValueError("شناسهٔ هدف نامعتبر است")
    if (data.plan_id is not None) != (data.scope == "plan") or (data.category_id is not None) != (data.scope == "category"):
        raise ValueError("هدف با محدودهٔ تخفیف مطابقت ندارد")
    if type(data.notify) is not bool or not isinstance(data.request_key, str) or len(data.request_key) != 32 or any(c not in "0123456789abcdef" for c in data.request_key):
        raise ValueError("درخواست نامعتبر است")


async def validate_target(session: AsyncSession, data: DiscountInput, shop_id: int | None) -> None:
    if data.scope == "plan":
        target = await session.scalar(select(Plan.id).where(
            Plan.id == data.plan_id, Plan.owner_reseller_id == shop_id,
            Plan.is_active.is_(True), Plan.is_trial.is_(False),
        ))
        if target is None:
            raise ValueError("پلن فعال این فروشگاه یافت نشد")
    if data.scope == "category":
        target = await session.scalar(select(PlanCategory.id).where(
            PlanCategory.id == data.category_id, PlanCategory.owner_reseller_id == shop_id,
            PlanCategory.is_active.is_(True), PlanCategory.audience == "users",
        ))
        if target is None:
            raise ValueError("دسته‌بندی این فروشگاه یافت نشد")


def announcement(row: CatalogDiscount, *, duration_hours: int | None) -> str:
    value = f"{format_number(row.reduction_value)}٪" if row.reduction_kind == "percent" else format_toman(row.reduction_value)
    heading = "کاهش دائمی قیمت‌ها" if row.mode == "permanent" else "تخفیف جدید"
    duration = "دائمی؛ بدون تاریخ پایان" if row.ends_at is None else f"{format_number(duration_hours)} ساعت؛ تا {expiry_input(row.ends_at)} (تهران)"
    return f"{heading}\n{row.title}\nمحدوده: {SCOPES[row.scope]}\nکاهش قیمت: {value}\nمدت: {duration}"


def recipient_query(shop_id: int | None) -> Select[tuple[int]]:
    shop_buyers = select(Order.user_id).where(Order.reseller_id == shop_id)
    membership = or_(BotUser.reseller_id == shop_id, BotUser.id.in_(shop_buyers))
    opted_out = exists(select(MarketingPreference.user_id).where(
        MarketingPreference.user_id == BotUser.id, MarketingPreference.shop_key == (shop_id or 0),
        MarketingPreference.enabled.is_(False),
    ))
    return select(BotUser.id).where(membership, ~opted_out, BotUser.is_blocked.is_(False), BotUser.role.in_(["user", "reseller"]))


async def enqueue_announcement(session: AsyncSession, row: CatalogDiscount, data: DiscountInput, staff: dict) -> None:
    campaign = TargetedCampaign(
        reseller_id=row.reseller_id, audience="catalog_discount", days=1,
        text=announcement(row, duration_hours=data.duration_hours), status="running",
        created_by=str(staff.get("username") or staff.get("bot_user_id") or "operator")[:128],
    )
    session.add(campaign)
    await session.flush()
    # Snapshot in SQL: no recipient cap or per-customer queries in a web request.
    recipients = recipient_query(row.reseller_id).with_only_columns(
        literal(campaign.id), BotUser.id,
    )
    await session.execute(insert(CampaignRecipient).from_select(["campaign_id", "user_id"], recipients))
    row.campaign_id = campaign.id


async def claimable_slot(session: AsyncSession, *, shop_id: int | None) -> int:
    await session.execute(update(CatalogDiscount).where(
        CatalogDiscount.shop_key == (shop_id or 0), CatalogDiscount.reseller_id == shop_id,
        CatalogDiscount.ends_at <= datetime.now(timezone.utc), CatalogDiscount.is_active.is_(True),
    ).values(is_active=False, active_slot=None))
    slots = set((await session.scalars(select(CatalogDiscount.active_slot).where(
        CatalogDiscount.shop_key == (shop_id or 0), CatalogDiscount.reseller_id == shop_id,
        CatalogDiscount.active_slot.is_not(None),
    ).limit(MAX_ACTIVE_DISCOUNTS))).all())
    available = set(range(MAX_ACTIVE_DISCOUNTS)) - slots
    if not available:
        raise ValueError("ابتدا یکی از تخفیف‌های فعال را متوقف کنید")
    return min(available)


async def create_discount(session: AsyncSession, staff: dict, data: DiscountInput) -> CatalogDiscount:
    shop_id = discount_scope(staff)
    validate_input(data)
    existing = await session.scalar(select(CatalogDiscount).where(
        CatalogDiscount.shop_key == (shop_id or 0), CatalogDiscount.reseller_id == shop_id,
        CatalogDiscount.request_key == data.request_key,
    ))
    if existing is not None:
        return existing
    await validate_target(session, data, shop_id)
    slot = await claimable_slot(session, shop_id=shop_id)
    ends_at = datetime.now(timezone.utc) + timedelta(hours=data.duration_hours) if data.mode == "temporary" else None
    row = CatalogDiscount(
        shop_key=shop_id or 0, reseller_id=shop_id, active_slot=slot, title=data.title.strip(), scope=data.scope,
        mode=data.mode, reduction_kind=data.reduction_kind, reduction_value=data.reduction_value,
        plan_id=data.plan_id, category_id=data.category_id, ends_at=ends_at, request_key=data.request_key,
    )
    try:
        session.add(row)
        await session.flush()
        if data.notify:
            await enqueue_announcement(session, row, data, staff)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(CatalogDiscount).where(
            CatalogDiscount.shop_key == (shop_id or 0), CatalogDiscount.reseller_id == shop_id,
            CatalogDiscount.request_key == data.request_key,
        ))
        if existing is None:
            raise ValueError("ثبت هم‌زمان قیمت؛ دوباره تلاش کنید") from None
        return existing
    return row


async def stop_discount(session: AsyncSession, staff: dict, discount_id: int) -> None:
    shop_id = discount_scope(staff)
    row = await session.scalar(select(CatalogDiscount).where(
        CatalogDiscount.id == discount_id, CatalogDiscount.reseller_id == shop_id,
        CatalogDiscount.shop_key == (shop_id or 0),
    ))
    if row is None:
        raise ValueError("تخفیف یافت نشد")
    await session.execute(update(CatalogDiscount).where(
        CatalogDiscount.id == discount_id, CatalogDiscount.reseller_id == shop_id,
        CatalogDiscount.shop_key == (shop_id or 0), CatalogDiscount.is_active.is_(True),
    ).values(is_active=False, active_slot=None))
    if row.campaign_id:
        await session.execute(update(TargetedCampaign).where(
            TargetedCampaign.id == row.campaign_id, TargetedCampaign.reseller_id == shop_id,
            TargetedCampaign.status.in_(["running", "paused", "draft"]),
        ).values(status="paused"))
    await session.commit()


def new_request_key() -> str:
    return secrets.token_hex(16)


async def pause_expired_announcements(session: AsyncSession) -> None:
    """Stop queued announcements after their linked offer expires or is stopped."""
    expired = exists(select(CatalogDiscount.id).where(
        CatalogDiscount.campaign_id == TargetedCampaign.id,
        or_(CatalogDiscount.reseller_id == TargetedCampaign.reseller_id,
            CatalogDiscount.reseller_id.is_(None) & TargetedCampaign.reseller_id.is_(None)),
        or_(CatalogDiscount.is_active.is_(False), CatalogDiscount.ends_at <= datetime.now(timezone.utc)),
        CatalogDiscount.campaign_id.is_not(None),
    ))
    await session.execute(update(TargetedCampaign).where(
        expired, TargetedCampaign.status == "running",
    ).values(status="paused"))
    await session.commit()


@dataclass(slots=True)
class DiscountDashboard:
    discounts: list[CatalogDiscount]
    plans: list[Plan]
    categories: list[PlanCategory]
    announcement_status: dict[int, str]
    next_before: int | None
    deadlines: dict[int, str]


async def discount_dashboard(session: AsyncSession, staff: dict, *, before: int | None = None) -> DiscountDashboard:
    shop_id = discount_scope(staff)
    query = select(CatalogDiscount).where(
        CatalogDiscount.reseller_id == shop_id, CatalogDiscount.shop_key == (shop_id or 0),
    )
    if before is not None:
        if type(before) is not int or not 1 <= before <= MAX_PRICE:
            raise ValueError("صفحه نامعتبر است")
        query = query.where(CatalogDiscount.id < before)
    rows = list((await session.scalars(query.order_by(CatalogDiscount.id.desc()).limit(51))).all())
    next_before = rows[49].id if len(rows) > 50 else None
    rows = rows[:50]
    plans = list((await session.scalars(select(Plan).where(
        Plan.owner_reseller_id == shop_id, Plan.is_active.is_(True), Plan.is_trial.is_(False),
    ).order_by(Plan.sort_order, Plan.id).limit(1000))).all())
    categories = list((await session.scalars(select(PlanCategory).where(
        PlanCategory.owner_reseller_id == shop_id, PlanCategory.is_active.is_(True), PlanCategory.audience == "users",
    ).order_by(PlanCategory.sort_order, PlanCategory.id).limit(1000))).all())
    campaign_ids = [row.campaign_id for row in rows if row.campaign_id is not None]
    statuses = dict((await session.execute(select(TargetedCampaign.id, TargetedCampaign.status).where(
        TargetedCampaign.id.in_(campaign_ids), TargetedCampaign.reseller_id == shop_id,
    ).limit(50))).all())
    deadlines = {row.id: expiry_input(row.ends_at) for row in rows}
    return DiscountDashboard(rows, plans, categories, statuses, next_before, deadlines)
