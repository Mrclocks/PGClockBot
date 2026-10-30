"""Buttons and notes for the low-time and low-volume alerts a customer receives.

The alert job sends one message per service, so everything here is computed per batch from a few
queries and the job only formats the result. A customer is offered what fits: a one-tap renewal, a
volume pack when the shop sells one, and a wallet top-up when automatic renewal is on but the wallet
of that shop cannot pay it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, ServiceAddonPack, ServiceAutomation, UserService
from app.services.button_styles import style_kwargs
from app.services.catalog_pricing import annotate_prices, sale_price
from app.services.formatting import format_toman
from app.services.service_automation import TOPUP_CALLBACK
from app.services.users import on
from app.services.wallet import wallet_balances_for_scope



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
