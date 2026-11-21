"""Buttons and notes for the low-time and low-volume alerts a customer receives.

The alert job sends one message per service, so everything here is computed per batch from a few
queries and the job only formats the result. A customer is offered what fits: renewal, a volume pack
when the shop sells one, a wallet top-up when automatic renewal is on but the wallet of that shop
cannot pay it, and always the way to their services and to support.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, ServiceAddonPack, ServiceAutomation, UserService
from app.services.catalog_pricing import annotate_prices, sale_price
from app.services.customer_notice_buttons import help_row, renew_button, volume_pack_button
from app.services.formatting import format_toman
from app.services.replaceable_notices import replace_notices
from app.services.users import on
from app.services.wallet import wallet_balances_for_scope

# How often the scheduler reads every active service for low-usage alerts; also shown in the «پر مصرف» note.
ALERT_CHECK_INTERVAL_HOURS = 6
ALERT_TIME = "time"
ALERT_TRAFFIC = "traffic"
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


PRICE_PREVIEW_KEY = "user_alert_price_preview_enabled"
# The wallet's preset-amount callback; the top-up flow validates the amount again.
TOPUP_AMOUNT_PREFIX = "nv:w:amt:"
# The smallest top-up the wallet accepts (orders.MIN_TOPUP_AMOUNT); a smaller shortfall rounds up to it.
TOPUP_STEP = 1000


@dataclass(frozen=True, slots=True)
class RenewQuote:
    """What renewing a service onto its own plan costs, and the wallet that would pay (None: wallet off)."""

    price: int
    balance: int | None

    @property
    def shortfall(self) -> int:
        return 0 if self.balance is None else max(0, self.price - max(0, self.balance))


@dataclass(slots=True)
class AlertExtras:
    """What a shop's alerts may offer: packs, the price of renewing, and short auto-renewal wallets."""

    sells_volume_packs: bool = False
    renew_shortfalls: dict[int, int] = field(default_factory=dict)
    renew_quotes: dict[int, RenewQuote] = field(default_factory=dict)
    wallet_balances: dict[int, int] = field(default_factory=dict)

    def note_for(self, service_id: int) -> str:
        """The renewal price or the wallet warning to append to an alert of this service, or ``""``."""
        price = self.renew_shortfalls.get(service_id)
        if price is not None:
            return shortfall_note(price)
        quote = self.renew_quotes.get(service_id)
        return quote_note(quote) if quote is not None else ""

    def topup_amount(self, service_id: int) -> int | None:
        """The exact top-up the alert offers, or None when the wallet can already pay."""
        price = self.renew_shortfalls.get(service_id)
        if price is not None:
            return rounded_topup(price - max(0, self.wallet_balances.get(service_id, 0)))
        quote = self.renew_quotes.get(service_id)
        return rounded_topup(quote.shortfall) if quote is not None and quote.shortfall else None


def rounded_topup(shortfall: int) -> int:
    return max(TOPUP_STEP, math.ceil(max(0, shortfall) / TOPUP_STEP) * TOPUP_STEP)


async def sells_volume_packs(session: AsyncSession, shop_id: int | None) -> bool:
    """True when the shop (None = platform) sells at least one active volume pack."""
    owner = ServiceAddonPack.owner_reseller_id.is_(None) if shop_id is None else ServiceAddonPack.owner_reseller_id == int(shop_id)
    found = await session.scalar(
        select(ServiceAddonPack.id)
        .where(owner, ServiceAddonPack.is_active.is_(True), ServiceAddonPack.kind == "volume")
        .limit(1)
    )
    return found is not None


async def _auto_renew_plans(session: AsyncSession, service_ids: list[int]) -> dict[int, int]:
    """service id -> chosen plan, for services whose automatic renewal can run."""
    rows = await session.execute(
        select(ServiceAutomation.service_id, ServiceAutomation.renew_plan_id).where(
            ServiceAutomation.service_id.in_(service_ids),
            ServiceAutomation.renew_enabled.is_(True),
            ServiceAutomation.needs_review.is_(False),
            ServiceAutomation.renew_plan_id.is_not(None),
        )
    )
    return {int(sid): int(plan_id) for sid, plan_id in rows.all()}


async def _sale_prices(session: AsyncSession, plan_ids: set[int], shop_id: int | None) -> dict[int, int]:
    """plan id -> sale price, for the shop's own active, non-trial plans among ``plan_ids``."""
    if not plan_ids:
        return {}
    # Only the shop's own plans can be bought there; a stale choice from another catalog is ignored.
    owner = Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)
    plans = list(await session.scalars(select(Plan).where(Plan.id.in_(plan_ids), owner)))
    await annotate_prices(session, plans, shop_id=shop_id)
    return {int(plan.id): sale_price(plan) for plan in plans if plan.is_active and not plan.is_trial}


def _renewable_plan(service: UserService) -> int | None:
    # A linked service is renewed on its own panel, not from this bot.
    if not service.plan_id or (service.remark or "").strip() == "linked":
        return None
    return int(service.plan_id)


async def alert_extras(
    session: AsyncSession, services: list[UserService], *, shop_id: int | None, ui: dict | None = None
) -> AlertExtras:
    """Extras for one shop's batch of services: a fixed number of queries whatever the batch size."""
    if not services:
        return AlertExtras()
    ui = ui or {}
    by_id = {int(service.id): service for service in services}
    auto_plans = await _auto_renew_plans(session, list(by_id))
    current = {sid: plan_id for sid, svc in by_id.items() if (plan_id := _renewable_plan(svc)) is not None}
    if not on(ui.get(PRICE_PREVIEW_KEY, "1")):
        current = {}
    prices = await _sale_prices(session, set(auto_plans.values()) | set(current.values()), shop_id)
    payers = {by_id[sid].bot_user_id for sid in set(auto_plans) | set(current)}
    balances = await wallet_balances_for_scope(session, list(payers), shop_id=shop_id) if payers else {}
    balance_of = {sid: int(balances.get(int(svc.bot_user_id), 0)) for sid, svc in by_id.items()}
    wallet_on = on(ui.get("pay_wallet_enabled", "1"))
    return AlertExtras(
        sells_volume_packs=await sells_volume_packs(session, shop_id),
        renew_shortfalls={
            sid: prices[plan_id] for sid, plan_id in auto_plans.items()
            if plan_id in prices and balance_of[sid] < prices[plan_id]
        },
        renew_quotes={
            sid: RenewQuote(price=prices[plan_id], balance=balance_of[sid] if wallet_on else None)
            for sid, plan_id in current.items() if prices.get(plan_id, 0) > 0
        },
        wallet_balances=balance_of,
    )


def shortfall_note(price: int) -> str:
    return (
        "\n⚠️ تمدید خودکار فعال است ولی موجودی کیف پول برای تمدید "
        f"({format_toman(price)}) کافی نیست؛ برای جلوگیری از قطع شدن، کیف پول را شارژ کنید."
    )


def quote_note(quote: RenewQuote) -> str:
    line = f"\n💳 تمدید همین پلن: <b>{format_toman(quote.price)}</b>"
    if quote.balance is None:
        return line
    line += f" · کیف پول شما: {format_toman(max(0, quote.balance))}"
    if quote.shortfall:
        line += f"\nبرای تمدید با کیف پول {format_toman(quote.shortfall)} کم دارید."
    return line


def _topup_row(extras: AlertExtras, service_id: int) -> list[list[InlineKeyboardButton]]:
    amount = extras.topup_amount(service_id)
    if amount is None:
        return []
    return [[InlineKeyboardButton(text=f"➕ شارژ {format_toman(amount)}", callback_data=f"{TOPUP_AMOUNT_PREFIX}{amount}")]]


def _renew_rows(ui: dict, service_id: int) -> list[list[InlineKeyboardButton]]:
    return [[renew_button(service_id, ui)]] if on(ui.get("one_tap_renew_enabled", "1")) else []


def time_alert_markup(ui: dict, service_id: int, extras: AlertExtras) -> InlineKeyboardMarkup:
    rows = _renew_rows(ui, service_id) + _topup_row(extras, service_id)
    return InlineKeyboardMarkup(inline_keyboard=[*rows, help_row()])


def traffic_alert_markup(ui: dict, service_id: int, extras: AlertExtras) -> InlineKeyboardMarkup:
    rows = [[volume_pack_button(service_id)]] if extras.sells_volume_packs else []
    rows += _renew_rows(ui, service_id) + _topup_row(extras, service_id)
    return InlineKeyboardMarkup(inline_keyboard=[*rows, help_row()])


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


def traffic_remaining_pct(used: float, limit: float) -> float | None:
    """Share of the volume still left (0 to 100), or ``None`` for an unlimited service."""
    if limit <= 0:
        return None
    return max(0.0, (1.0 - used / limit) * 100.0)


async def plan_durations(session: AsyncSession, services: list[UserService]) -> dict[int, int]:
    """Plan id -> duration in days for the given services, in one query."""
    plan_ids = {int(s.plan_id) for s in services if s.plan_id}
    if not plan_ids:
        return {}
    rows = (await session.execute(select(Plan.id, Plan.duration_days).where(Plan.id.in_(plan_ids)))).all()
    return {int(pid): int(days) for pid, days in rows if days}
