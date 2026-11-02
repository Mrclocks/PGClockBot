"""What the customer is paying for, shown above the payment methods of a new purchase.

The bot used to show these details on a separate confirm screen before the order was
created. That screen is gone, so the payment screen carries them instead, and every way
back to it (a discount code, a cancelled input) shows the same summary.
"""

from __future__ import annotations

import html

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Plan
from app.services.catalog_pricing import active_discounts, price_html, product_target, quote_price
from app.services.formatting import format_message, format_plan_duration_days, format_toman, info_block, kv_line
from app.services.orders import CUSTOM_ORDER_NOTE

_SERVICE_NOTES = ("renew:", "svc_addon:", "reseller_app:")


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


async def checkout_summary(
    session: AsyncSession, order: Order, *, currency: str, notice: str | None = None
) -> str:
    """HTML summary of ``order`` for the payment screen.

    ``notice`` is an already-escaped line such as «تخفیف اعمال شد ✅». Renewals, add-ons
    and reseller applications keep their own context and get the order id and amount only.
    """
    plan = await session.get(Plan, int(order.plan_id)) if order.plan_id else None
    lines = await _plan_lines(session, plan, order, currency) if plan is not None and _is_new_purchase(order) else []
    discount = int(order.discount_amount or 0)
    if discount > 0:
        lines.append(kv_line("🏷", "تخفیف", f"−{format_toman(discount, currency)}"))
    if notice:
        lines.append(notice)
    lines.append(kv_line("💰", "مبلغ قابل پرداخت", f"<b>{format_toman(order.amount, currency)}</b>"))
    title = f"🧾 سفارش عمده #{order.id}" if int(order.quantity or 1) > 1 else f"🧾 سفارش #{order.id}"
    return format_message(title, info_block(lines))
