"""Web route that voids an approved payment (separate permission, shop-scoped)."""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.db.models import Payment
from app.services.authz import authz_from_staff, can_shop
from app.services.formatting import format_toman
from app.services.panel_sidebar_cache import invalidate_sidebar_counts, sidebar_cache_key
from app.services.payer_notify import notify_payer
from app.services.payment_voids import STATUS_DONE, VOID_REASON_MAX, list_voids, void_approved_payment
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

logger = logging.getLogger(__name__)

PERMISSION = "payment_void"
FINANCE_PAYMENTS_PATH = "/finance?tab=payments"
FLASH_LIMIT = 180


def void_scope(staff: dict) -> int | None:
    """Shop the staff member may void payments for; 403 without the permission or a shop scope."""
    if not can_shop(authz_from_staff(staff), PERMISSION):
        raise HTTPException(403)
    try:
        return resolve_shop_scope_id(staff)
    except ShopScopeError:
        raise HTTPException(403) from None


def _redirect(*, ok: str = "", err: str = "") -> RedirectResponse:
    key, text = ("ok", ok) if ok else ("err", err)
    text = " ".join(text.split())[:FLASH_LIMIT]
    suffix = f"&{key}={quote(text)}" if text else ""
    return RedirectResponse(FINANCE_PAYMENTS_PATH + suffix, status_code=303)


def _actor(staff: dict) -> str:
    return str(staff.get("org_principal_id") or staff.get("username") or staff.get("bot_user_id") or "operator")


def _customer_notice(payment: Payment, refund_amount: int) -> str:
    if payment.is_wallet_topup:
        return f"شارژ کیف پول #{payment.id} ابطال شد و مبلغ آن از کیف پول شما کم شد. اگر اشتباهی رخ داده با پشتیبانی تماس بگیرید."
    text = f"پرداخت #{payment.id} ابطال و سرویس مربوط به آن غیرفعال شد."
    if refund_amount:
        text += f" مبلغ {format_toman(refund_amount)} به کیف پول شما برگشت."
    return text + " اگر اشتباهی رخ داده با پشتیبانی تماس بگیرید."


def register_payment_void_pages(app, *, require_staff, get_db) -> None:
    @app.post("/payments/{payment_id}/void")
    async def payment_void(
        request: Request, payment_id: int, staff: dict = Depends(require_staff), session=Depends(get_db)
    ):
        scope = void_scope(staff)
        form = await request.form()
        reason = str(form.get("reason", ""))[: VOID_REASON_MAX * 2]
        refund = str(form.get("refund_to_wallet", "")) == "1"
        previous = (await list_voids(session, [payment_id])).get(payment_id)
        try:
            void = await void_approved_payment(
                session, payment_id, shop_id=scope, actor=_actor(staff), reason=reason, refund_to_wallet=refund
            )
        except ValueError as exc:
            await session.rollback()
            return _redirect(err=str(exc))
        invalidate_sidebar_counts(sidebar_cache_key(staff))
        if previous is None or previous.status != STATUS_DONE:
            payment = await session.get(Payment, payment_id)
            await notify_payer(
                session, payment.user_id, title="⚠️ پرداخت ابطال شد",
                body=_customer_notice(payment, int(void.refund_amount)), shop_id=void.shop_id,
            )
        return _redirect(ok="پرداخت ابطال شد")
