"""Failed-delivery list actions on one order: remove the row, or close a stuck order (refund, manual delivery, close)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.delivery_failure_queue import NOTHING_TO_DISMISS, dismiss_delivery_failures
from app.services.payer_notify import notify_payer
from app.services.shop_scope import ShopScopeError
from app.services.stuck_order_resolution import (
    Resolution,
    cancel_and_refund,
    close_without_refund,
    count_still_stuck,
    mark_delivered_manually,
)

LIST_URL = "/finance?tab=delivery"
DISMISSED_NOTICE = "ردیف خطا از لیست حذف شد"
STILL_STUCK_NOTICE = (
    "ردیف خطا حذف شد، ولی این سفارش هنوز پرداخت‌شده و بدون سرویس است و در لیست «گیرکرده» می‌ماند. "
    "برای بستن آن «لغو و بازگشت وجه» یا «تحویل دستی انجام شد» را بزنید."
)
REFUND_NOTICE_TITLE = "↩️ بازگشت وجه"


def _redirect(*, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    key, text = ("ok", ok) if ok else ("err", err or "")
    return RedirectResponse(f"{LIST_URL}&{key}={quote(text)}", status_code=303)


def _actor(staff: dict) -> str:
    return f"{staff.get('role') or 'staff'}:{staff.get('username') or staff.get('bot_user_id') or ''}"


def register_delivery_failure_pages(app: FastAPI, *, require_orders: Callable, get_db: Callable) -> None:
    @app.post("/orders/{order_id}/dismiss-delivery-failure")
    async def dismiss_delivery_failure(
        order_id: int,
        staff: dict = Depends(require_orders),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        dismissed, _skipped = await dismiss_delivery_failures(session, staff, [order_id])
        if not dismissed:
            return _redirect(err=NOTHING_TO_DISMISS)
        stuck = await count_still_stuck(session, [order_id])
        return _redirect(ok=STILL_STUCK_NOTICE if stuck else DISMISSED_NOTICE)

    @app.post("/orders/{order_id}/refund-stuck-order")
    async def refund_stuck_order(
        order_id: int,
        staff: dict = Depends(require_orders),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            result = await cancel_and_refund(session, staff, order_id, actor=_actor(staff))
        except (ValueError, ShopScopeError) as exc:
            return _redirect(err=getattr(exc, "message", None) or str(exc))
        await _tell_customer(session, result)
        text = f"سفارش #{result.order_id} لغو شد و {result.refunded:,} به کیف پول مشتری برگشت" if result.refunded else f"سفارش #{result.order_id} لغو شد (مبلغی پرداخت نشده بود)"
        return _redirect(ok=text)

    @app.post("/orders/{order_id}/mark-delivered-manually")
    async def mark_delivered(
        order_id: int,
        staff: dict = Depends(require_orders),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            result = await mark_delivered_manually(session, staff, order_id, actor=_actor(staff))
        except (ValueError, ShopScopeError) as exc:
            return _redirect(err=getattr(exc, "message", None) or str(exc))
        return _redirect(ok=f"سفارش #{result.order_id} به‌عنوان تحویل دستی ثبت شد")

    @app.post("/orders/{order_id}/close-stuck-order")
    async def close_stuck_order(
        order_id: int,
        staff: dict = Depends(require_orders),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            result = await close_without_refund(session, staff, order_id, actor=_actor(staff))
        except (ValueError, ShopScopeError) as exc:
            return _redirect(err=getattr(exc, "message", None) or str(exc))
        return _redirect(ok=f"سفارش #{result.order_id} بدون بازگشت وجه بسته و از لیست حذف شد")


async def _tell_customer(session: AsyncSession, result: Resolution) -> None:
    if result.refunded <= 0:
        return
    await notify_payer(
        session, result.user_id, title=REFUND_NOTICE_TITLE,
        body=f"سفارش #{result.order_id} به‌دلیل انجام‌نشدن تحویل لغو شد و {result.refunded:,} به کیف پول شما برگشت.",
        shop_id=result.shop_id,
    )
