"""Optional expiry of unfinished purchases: cancel an order nobody has paid for after a set number of hours.

Off by default; each shop (and the platform) switches it on in finance settings → cleanup, with its own number of
hours. It is not the retired soft-cancel (``stale_order_cleanup``): it cancels only what its customer could cancel
themselves (``checkout_cancel``): no approved payment, no receipt, no online payment in progress. That rule and the
age are part of the atomic status claim, so a receipt arriving during the run stops the cancel. Each order is one
unit of work, committed alone together with a customer notice («خرید نیمه‌تمام لغو شد», sent by the notice
worker through the shop's own bot). Deleting old rows stays with ``order_purge``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, ResellerSetting
from app.services.checkout_cancel import CUSTOMER_CANCEL_STATUSES, customer_cancellable_clause
from app.services.customer_notice_queue import add_notice
from app.services.db_safe import rollback_quiet
from app.services.order_expiry_texts import EXPIRED_ACTION
from app.services.orders import cancel_order
from app.services.users import get_all_settings, on, set_settings_bulk

logger = logging.getLogger(__name__)

KEY_ENABLED = "pending_order_expiry_enabled"
KEY_HOURS = "pending_order_expiry_hours"
DEFAULT_HOURS = 48
MIN_HOURS = 1
MAX_HOURS = 720
ORDERS_PER_SHOP = 100
SHOP_SCAN_PAGE = 2000
EXPIRY_NOTE = "unpaid order expired"
HOURS_ERROR = f"مهلت لغو خودکار باید عدد صحیح بین {MIN_HOURS} و {MAX_HOURS} ساعت باشد"


@dataclass(frozen=True, slots=True)
class ExpiryRules:
    enabled: bool = False
    hours: int = DEFAULT_HOURS


def parse_hours(raw: object) -> int:
    """Whole hours between the limits; ``ValueError`` with a safe message otherwise."""
    text = str(raw or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 4:
        raise ValueError(HOURS_ERROR)
    hours = int(text)
    if not MIN_HOURS <= hours <= MAX_HOURS:
        raise ValueError(HOURS_ERROR)
    return hours


def rules_from_settings(settings: Mapping[str, str]) -> ExpiryRules:
    try:
        hours = parse_hours(settings.get(KEY_HOURS, DEFAULT_HOURS))
    except ValueError:
        hours = DEFAULT_HOURS
    return ExpiryRules(enabled=on(settings.get(KEY_ENABLED, "0")), hours=hours)


def rules_from_form(form: Mapping[str, object]) -> ExpiryRules | None:
    """The rules a cleanup form sets; ``None`` when the form has no expiry fields (they stay as saved)."""
    if "expiry_hours" not in form:
        return None
    return ExpiryRules(enabled=str(form.get("expiry") or "") == "1", hours=parse_hours(form.get("expiry_hours")))


async def save_expiry_rules(session: AsyncSession, *, shop_id: int | None, rules: ExpiryRules) -> None:
    await set_settings_bulk(
        session, {KEY_ENABLED: "1" if rules.enabled else "0", KEY_HOURS: str(rules.hours)}, reseller_id=shop_id
    )


def expiry_panel_context(settings: Mapping[str, str]) -> dict[str, object]:
    rules = rules_from_settings(settings)
    return {"on": rules.enabled, "hours": rules.hours, "min_hours": MIN_HOURS, "max_hours": MAX_HOURS}


def _shop_clause(shop_id: int | None):
    return Order.reseller_id.is_(None) if shop_id is None else Order.reseller_id == int(shop_id)


def _expired_clause(cutoff: datetime):
    return and_(
        Order.status.in_(CUSTOMER_CANCEL_STATUSES),
        Order.created_at < cutoff,
        customer_cancellable_clause(Order.id),
    )


async def _expire_one(session: AsyncSession, order: Order, telegram_id: int | None, cutoff: datetime) -> bool:
    """Cancel one expired order and queue its notice in one commit; False when it no longer qualifies."""
    order_id = int(order.id)
    if telegram_id:
        add_notice(
            session, shop_id=order.reseller_id, bot_user_id=int(order.user_id), telegram_id=int(telegram_id),
            action=EXPIRED_ACTION, service_name=f"#{order_id}", reason="",
        )
    try:
        await cancel_order(session, order, note=EXPIRY_NOTE, only_if=_expired_clause(cutoff))
    except ValueError:
        # Paid, receipt sent or cancelled meanwhile: the claim matched nothing; drop the queued notice too.
        await rollback_quiet(session)
        return False
    except SQLAlchemyError:
        logger.warning("order expiry failed order=%s", order_id, exc_info=True)
        await rollback_quiet(session)
        return False
    return True


async def expire_shop_orders(
    session: AsyncSession, *, shop_id: int | None, hours: int, now: datetime | None = None
) -> int:
    """Cancel up to ``ORDERS_PER_SHOP`` of the shop's unpaid orders older than ``hours``; how many were cancelled."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=int(hours))
    rows = (await session.execute(
        select(Order.id, BotUser.telegram_id)
        .join(BotUser, BotUser.id == Order.user_id)
        .where(_shop_clause(shop_id), _expired_clause(cutoff))
        .order_by(Order.id)
        .limit(ORDERS_PER_SHOP)
    )).all()
    expired = 0
    # Ids only: a rollback of one order expires every loaded row, so each order is read fresh.
    for order_id, telegram_id in rows:
        order = await session.get(Order, int(order_id), populate_existing=True)
        if order is not None and await _expire_one(session, order, telegram_id, cutoff):
            expired += 1
    return expired


async def _shops_with_expiry(session: AsyncSession) -> list[int]:
    """Every shop that switched expiry on, keyset-paged so the scan is not capped at one page."""
    shops: list[int] = []
    after_id = 0
    while True:
        page = list((await session.scalars(
            select(ResellerSetting.reseller_user_id)
            .where(
                ResellerSetting.key == KEY_ENABLED,
                ResellerSetting.value.in_(("1", "true", "yes", "on")),
                ResellerSetting.reseller_user_id > after_id,
            )
            .order_by(ResellerSetting.reseller_user_id)
            .limit(SHOP_SCAN_PAGE)
        )).all())
        shops.extend(int(shop_id) for shop_id in page)
        if len(page) < SHOP_SCAN_PAGE:
            return shops
        after_id = int(page[-1])


async def expire_all_shops(session: AsyncSession, *, now: datetime | None = None) -> int:
    """One run for the platform and every shop with expiry on; one failing shop does not stop the others."""
    tenants: list[int | None] = [None, *await _shops_with_expiry(session)]
    total = 0
    for shop_id in tenants:
        try:
            # 0 pins the platform's own settings (None would follow the request's shop).
            rules = rules_from_settings(await get_all_settings(session, reseller_id=shop_id or 0))
            if rules.enabled:
                total += await expire_shop_orders(session, shop_id=shop_id, hours=rules.hours, now=now)
        except SQLAlchemyError:
            logger.warning("order expiry failed shop=%s", shop_id, exc_info=True)
            await rollback_quiet(session)
    return total
