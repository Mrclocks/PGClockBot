"""What the customer is paying for, shown above the payment methods of a new purchase.

The bot used to show these details on a separate confirm screen before the order was
created. That screen is gone, so the payment screen carries them instead, and every way
back to it (a discount code, a cancelled input) shows the same summary.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Plan
from app.services.catalog_pricing import active_discounts, price_html, product_target, quote_price
from app.services.formatting import copyable, format_message, format_plan_duration_days, format_toman, info_block, kv_line
from app.services.custom_service_name import preview_username
from app.services.cashback import quote_cashback
from app.services.db_safe import rollback_quiet
from app.services.orders import CUSTOM_ORDER_NOTE

logger = logging.getLogger(__name__)

_SERVICE_NOTES = ("renew:", "svc_addon:", "reseller_app:", "res_cap:", "res_pkg:")


def _is_new_purchase(order: Order) -> bool:
    note = (order.note or "").strip()
    return not note.startswith(_SERVICE_NOTES)


def _volume(plan: Plan) -> str:
    return f"{plan.data_limit_gb:g} گیگابایت" if plan.data_limit_gb else "نامحدود"


async def _plan_lines(session: AsyncSession, plan: Plan, order: Order, currency: str) -> list[str]:
    custom = (order.note or "").strip() == CUSTOM_ORDER_NOTE
    quantity = int(order.quantity or 1)
    each = " هر سرویس" if quantity > 1 else ""
    lines = [kv_line("💎", "پلن", f"<b>{html.escape(plan.name or 'پلن')}</b>")]
    if plan.description and not custom:
        lines.append(html.escape(plan.description.strip()))
    lines += [
        kv_line("⏱", f"مدت{each}", f"<b>{format_plan_duration_days(plan.duration_days)}</b>"),
        kv_line("📦", f"حجم{each}", f"<b>{_volume(plan)}</b>"),
    ]
    if quantity > 1:
        lines.append(kv_line("🔢", "تعداد", f"<b>{quantity}</b>"))
    if not custom:
        # The plan's catalog price: crossed out with its deadline while a discount runs.
        quote = quote_price(product_target(plan), await active_discounts(session, shop_id=order.reseller_id))
        if quote.original_price is not None or quantity > 1:
            lines.append(kv_line("🏷", "قیمت هر سرویس" if quantity > 1 else "قیمت", price_html(quote, currency)))
    return lines


async def _cashback_line(session: AsyncSession, order: Order, currency: str) -> str | None:
    """«کش‌بک این خرید» from the same rules that grant it; a failure never hides the checkout."""
    try:
        _rule_id, amount = await quote_cashback(session, order, at=datetime.now(timezone.utc))
    except SQLAlchemyError:
        logger.warning("cashback preview failed order=%s", order.id, exc_info=True)
        await rollback_quiet(session)
        return None
    if amount <= 0:
        return None
    return kv_line("🎁", "کش‌بک این خرید", f"<b>{format_toman(amount, currency)}</b> (بعد از تحویل، در کیف پول)")


def _discount_lines(order: Order, currency: str) -> list[str]:
    random_discount = int(order.random_discount_amount or 0)
    reductions = (
        (int(order.discount_amount or 0) - random_discount, "تخفیف"),
        (random_discount, "تخفیف تصادفی"),
    )
    return [
        kv_line("🏷", label, f"−{format_toman(amount, currency)}")
        for amount, label in reductions if amount > 0
    ]


async def checkout_summary(
    session: AsyncSession, order: Order, *, currency: str, notice: str | None = None
) -> str:
    """HTML summary of ``order`` for the payment screen.

    ``notice`` is an already-escaped line such as «تخفیف اعمال شد ✅». Renewals, add-ons
    and reseller applications keep their own context and get the order id and amount only.
    """
    plan = await session.get(Plan, int(order.plan_id)) if order.plan_id else None
    lines = await _plan_lines(session, plan, order, currency) if plan is not None and _is_new_purchase(order) else []
    if order.requested_username:
        username = await preview_username(session, order, order.requested_username)
        lines.append(kv_line("✏️", "نام سرویس", copyable(username)))
    lines.extend(_discount_lines(order, currency))
    if notice:
        lines.append(notice)
    lines.append(kv_line("💰", "مبلغ قابل پرداخت", f"<b>{format_toman(order.amount, currency)}</b>"))
    cashback = await _cashback_line(session, order, currency)
    if cashback:
        lines.append(cashback)
    title = f"🧾 سفارش عمده #{order.id}" if int(order.quantity or 1) > 1 else f"🧾 سفارش #{order.id}"
    return format_message(title, info_block(lines))
