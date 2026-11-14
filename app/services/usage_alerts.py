"""Buttons and notes for the low-time and low-volume alerts a customer receives.

The alert job sends one message per service, so everything here is computed per batch from a few
queries and the job only formats the result. A customer is offered what fits: a one-tap renewal, a
volume pack when the shop sells one, and a wallet top-up when automatic renewal is on but the wallet
of that shop cannot pay it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, ServiceAddonPack, ServiceAutomation, UserService
from app.services.button_styles import style_kwargs
from app.services.catalog_pricing import annotate_prices, sale_price
from app.services.formatting import format_toman
from app.services.replaceable_notices import replace_notices
from app.services.service_automation import TOPUP_CALLBACK
from app.services.users import on
from app.services.wallet import wallet_balances_for_scope

ALERT_TIME = "time"
ALERT_TRAFFIC = "traffic"
# An alert that already went out is armed again only when the remaining share climbs this far above
# its threshold, so a service hovering around the threshold is not alerted over and over.
REARM_MARGIN_PCT = 5.0
SECONDS_PER_DAY = 86400
# (bot that sent it, replace key, the alert message)
SentAlert = tuple[Bot, str, Message]


def usage_alert_key(kind: str, service_id: int) -> str:
    """One low-time and one low-volume alert per service: the next one of a kind replaces the last."""
    return f"usage:{kind}:{int(service_id)}"


async def replace_previous_alerts(session: AsyncSession, sent: list[SentAlert]) -> None:
    """Retire the earlier alert of the same kind and service in each customer's chat.

    Called after the batch is committed, so a rolled-back batch never deletes an alert.
    """
    for bot, key, message in sent:
        await replace_notices(session, bot, key, [message])


@dataclass(slots=True)
class AlertExtras:
    """What a shop's alerts may offer: pack availability and the services whose auto-renewal is short."""

    sells_volume_packs: bool = False
    renew_shortfalls: dict[int, int] = field(default_factory=dict)

    def note_for(self, service_id: int) -> str:
        """The wallet warning to append to an alert of this service, or an empty string."""
        price = self.renew_shortfalls.get(service_id)
        return shortfall_note(price) if price is not None else ""


async def _sells_volume_packs(session: AsyncSession, shop_id: int | None) -> bool:
    owner = ServiceAddonPack.owner_reseller_id.is_(None) if shop_id is None else ServiceAddonPack.owner_reseller_id == int(shop_id)
    found = await session.scalar(
        select(ServiceAddonPack.id)
        .where(owner, ServiceAddonPack.is_active.is_(True), ServiceAddonPack.kind == "volume")
        .limit(1)
    )
    return found is not None


async def _renew_shortfalls(session: AsyncSession, services: list[UserService], shop_id: int | None) -> dict[int, int]:
    """service id -> renewal price, for services with automatic renewal on whose wallet cannot pay it."""
    by_id = {int(service.id): service for service in services}
    rows = (
        await session.execute(
            select(ServiceAutomation.service_id, ServiceAutomation.renew_plan_id).where(
                ServiceAutomation.service_id.in_(list(by_id)),
                ServiceAutomation.renew_enabled.is_(True),
                ServiceAutomation.needs_review.is_(False),
                ServiceAutomation.renew_plan_id.is_not(None),
            )
        )
    ).all()
    if not rows:
        return {}
    # Only the shop's own plans can be bought by its automation; a stale choice from another catalog is ignored.
    owner = Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)
    plans = list(await session.scalars(select(Plan).where(Plan.id.in_({plan_id for _, plan_id in rows}), owner)))
    await annotate_prices(session, plans, shop_id=shop_id)
    prices = {int(plan.id): sale_price(plan) for plan in plans if plan.is_active and not plan.is_trial}
    balances = await wallet_balances_for_scope(session, [by_id[sid].bot_user_id for sid, _ in rows], shop_id=shop_id)
    return {
        int(sid): prices[plan_id]
        for sid, plan_id in rows
        if plan_id in prices and balances.get(int(by_id[sid].bot_user_id), 0) < prices[plan_id]
    }


async def alert_extras(session: AsyncSession, services: list[UserService], *, shop_id: int | None) -> AlertExtras:
    """Extras for one shop's batch of services: two or three queries whatever the batch size."""
    if not services:
        return AlertExtras()
    return AlertExtras(
        sells_volume_packs=await _sells_volume_packs(session, shop_id),
        renew_shortfalls=await _renew_shortfalls(session, services, shop_id),
    )


def shortfall_note(price: int) -> str:
    return (
        "\n⚠️ تمدید خودکار فعال است ولی موجودی کیف پول برای تمدید "
        f"({format_toman(price)}) کافی نیست؛ برای جلوگیری از قطع شدن، کیف پول را شارژ کنید."
    )


def _renew_button(ui: dict, service_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="🔄 تمدید یک‌ضربی", callback_data=f"svc:renew:{service_id}", **style_kwargs(ui, "one_tap_renew", fallback="primary")
    )


def _topup_row(extras: AlertExtras, service_id: int) -> list[list[InlineKeyboardButton]]:
    if service_id not in extras.renew_shortfalls:
        return []
    return [[InlineKeyboardButton(text="➕ شارژ کیف پول", callback_data=TOPUP_CALLBACK)]]


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def time_alert_markup(ui: dict, service_id: int, extras: AlertExtras) -> InlineKeyboardMarkup | None:
    rows = [[_renew_button(ui, service_id)]] if on(ui.get("one_tap_renew_enabled", "1")) else []
    return _markup(rows + _topup_row(extras, service_id))


def traffic_alert_markup(ui: dict, service_id: int, extras: AlertExtras) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if extras.sells_volume_packs:
        rows.append([InlineKeyboardButton(text="📦 خرید بستهٔ حجم", callback_data=f"svc:addon:{service_id}:volume")])
    if on(ui.get("one_tap_renew_enabled", "1")):
        rows.append([_renew_button(ui, service_id)])
    return _markup(rows + _topup_row(extras, service_id))


def period_seconds(*, plan_days: int | None, created_at: datetime | None, expire: datetime) -> float | None:
    """Length of the current period in seconds: the plan's duration, else the time since creation.

    The creation date alone is wrong once a service was renewed: it would count every earlier
    period too, so the share of time left would stay high and the alert would come late.
    """
    if plan_days and plan_days > 0:
        return float(plan_days) * SECONDS_PER_DAY
    if created_at is not None and expire > created_at:
        return (expire - created_at).total_seconds()
    return None


def time_remaining_pct(expire: datetime, *, now: datetime, period: float | None) -> float | None:
    """Share of the period still left (0 to 100), or ``None`` when it cannot be told or already ended."""
    if not period or period <= 0:
        return None
    remaining = (expire - now).total_seconds()
    if remaining < 0:
        return None
    return min(100.0, remaining / period * 100.0)


def traffic_remaining_pct(used: float, limit: float) -> float | None:
    """Share of the volume still left (0 to 100), or ``None`` for an unlimited service."""
    if limit <= 0:
        return None
    return max(0.0, (1.0 - used / limit) * 100.0)


def should_rearm(remaining_pct: float | None, threshold_pct: float) -> bool:
    """True when an alert that was already sent must be allowed to fire again."""
    return remaining_pct is not None and remaining_pct > threshold_pct + REARM_MARGIN_PCT


async def plan_durations(session: AsyncSession, services: list[UserService]) -> dict[int, int]:
    """Plan id -> duration in days for the given services, in one query."""
    plan_ids = {int(s.plan_id) for s in services if s.plan_id}
    if not plan_ids:
        return {}
    rows = (await session.execute(select(Plan.id, Plan.duration_days).where(Plan.id.in_(plan_ids)))).all()
    return {int(pid): int(days) for pid, days in rows if days}
