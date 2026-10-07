"""Settlement HTTP endpoints — fail-closed, tenant-scoped, mock gated.

Public surface:
  - Real PSP return (provider != mock) — verifies via provider API
  - Card-auto webhooks — per-tenant path + HMAC secret from that tenant only
  - Mock checkout/pay — ONLY when ALLOW_SETTLEMENT_MOCK=1 + one-time token
"""

from __future__ import annotations

import logging
from html import escape

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Payment, SettlementStatus

logger = logging.getLogger(__name__)


def _html_page(title: str, body: str, *, ok: bool = True, status_code: int = 200) -> HTMLResponse:
    color = "#16a34a" if ok else "#dc2626"
    doc = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{escape(title)}</title>
<style>
body{{font-family:Tahoma,Arial,sans-serif;background:#f4f4f5;margin:0;padding:24px;color:#18181b}}
.card{{max-width:420px;margin:40px auto;background:#fff;border-radius:12px;padding:24px;
box-shadow:0 1px 3px rgba(0,0,0,.08);text-align:center}}
h1{{font-size:1.15rem;margin:0 0 12px;color:{color}}}
p{{margin:8px 0;line-height:1.7;font-size:.95rem}}
.btn{{display:inline-block;margin-top:16px;padding:10px 20px;background:#2563eb;color:#fff;
text-decoration:none;border:0;border-radius:8px;font:inherit;cursor:pointer}}
.muted{{color:#71717a;font-size:.85rem}}
</style>
</head>
<body><div class="card">{body}</div></body></html>"""
    return HTMLResponse(doc, status_code=status_code)


def _sig_from_headers(request: Request) -> str:
    signature = (
        request.headers.get("x-signature")
        or request.headers.get("x-card-auto-signature")
        or request.headers.get("x-hub-signature-256")
        or ""
    )
    if signature.lower().startswith("sha256="):
        signature = signature.split("=", 1)[1]
    return signature.strip()


def register_settlement_pages(app, *, get_db):
    @app.get("/payments/settlement/mock/checkout", response_class=HTMLResponse)
    async def mock_checkout_page(
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        from app.db.models import PaymentSettlement
        from app.services.payment_settlement import CHANNEL_PSP, settlement_mock_allowed

        if not settlement_mock_allowed():
            return Response(status_code=404, content="Not Found")

        sid = request.query_params.get("settlement_id") or ""
        token = (request.query_params.get("token") or "").strip()
        try:
            settlement_id = int(sid)
        except (TypeError, ValueError):
            return _html_page("خطا", "<h1>درخواست نامعتبر</h1>", ok=False, status_code=400)
        settlement = await session.get(PaymentSettlement, settlement_id)
        if (
            not settlement
            or settlement.channel != CHANNEL_PSP
            or settlement.provider != "mock"
        ):
            return _html_page("خطا", "<h1>درخواست نامعتبر</h1>", ok=False, status_code=404)
        # Constant-time-ish token check without leaking existence details on mismatch.
        from app.services.payment_settlement import _require_mock_token

        try:
            _require_mock_token(settlement, token)
        except ValueError:
            return _html_page("خطا", "<h1>درخواست نامعتبر</h1>", ok=False, status_code=403)

        if settlement.status == SettlementStatus.SETTLED.value:
            return _html_page("پرداخت شده", "<h1>قبلاً تسویه شده</h1>")

        amount = int(settlement.amount)
        # Do not echo payment_id / internal ids beyond what the payer already has in the URL.
        body = f"""
<h1>درگاه آزمایشی (Mock)</h1>
<p>مبلغ: <b>{amount:,}</b> تومان</p>
<form method="post" action="/payments/settlement/mock/pay">
  <input type="hidden" name="settlement_id" value="{settlement.id}"/>
  <input type="hidden" name="amount" value="{amount}"/>
  <input type="hidden" name="token" value="{escape(token)}"/>
  <button class="btn" type="submit">پرداخت آزمایشی موفق</button>
</form>
<p class="muted">فقط با ALLOW_SETTLEMENT_MOCK=1</p>
"""
        return _html_page("Mock PSP", body)

    @app.post("/payments/settlement/mock/pay", response_class=HTMLResponse)
    async def mock_pay(
        settlement_id: int = Form(...),
        amount: int = Form(...),
        token: str = Form(...),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.payment_settlement import (
            mock_psp_pay,
            notify_after_settlement,
            settlement_mock_allowed,
        )

        if not settlement_mock_allowed():
            return Response(status_code=404, content="Not Found")
        try:
            settlement = await mock_psp_pay(
                session,
                settlement_id=settlement_id,
                amount=amount,
                token=token,
            )
        except ValueError:
            return _html_page(
                "ناموفق",
                "<h1>تسویه ناموفق</h1><p class='muted'>درخواست رد شد.</p>",
                ok=False,
                status_code=400,
            )
        payment = await session.get(Payment, int(settlement.payment_id))
        order = None
        if payment and payment.order_id:
            order = await session.get(Order, payment.order_id)
        if payment:
            try:
                await notify_after_settlement(session, payment, order)
            except Exception:
                logger.exception("mock pay notify failed payment=%s", payment.id)
        return _html_page(
            "موفق",
            "<h1>پرداخت آزمایشی تأیید شد</h1>"
            "<p>به ربات برگردید.</p>",
        )

    @app.get("/payments/settlement/psp/{provider}/return/{settlement_id}")
    async def psp_return(
        provider: str,
        settlement_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        from app.db.models import PaymentSettlement
        from app.services.payment_settlement import (
            complete_psp_return,
            notify_after_settlement,
            settlement_mock_allowed,
        )

        provider_l = (provider or "").strip().lower()
        if provider_l == "mock" and not settlement_mock_allowed():
            return Response(status_code=404, content="Not Found")

        params = dict(request.query_params)
        pre = await session.get(PaymentSettlement, settlement_id)
        if not pre:
            return _html_page(
                "ناموفق",
                "<h1>تسویه یافت نشد</h1>",
                ok=False,
                status_code=404,
            )
        reseller_id = pre.shop_owner_id

        try:
            settlement = await complete_psp_return(
                session,
                provider=provider_l,
                settlement_id=settlement_id,
                callback_params=params,
                reseller_id=reseller_id,
            )
        except ValueError:
            return _html_page(
                "ناموفق",
                "<h1>تأیید پرداخت ناموفق</h1>"
                "<p class='muted'>اگر مبلغ کم شده با پشتیبانی تماس بگیرید.</p>",
                ok=False,
                status_code=400,
            )
        payment = await session.get(Payment, int(settlement.payment_id))
        order = None
        if payment and payment.order_id:
            order = await session.get(Order, payment.order_id)
        if payment and settlement.status == SettlementStatus.SETTLED.value:
            try:
                await notify_after_settlement(session, payment, order)
            except Exception:
                logger.exception("psp return notify failed payment=%s", payment.id)
        return _html_page(
            "موفق",
            "<h1>پرداخت تأیید شد</h1><p>می‌توانید به تلگرام برگردید.</p>",
        )

    async def _card_auto_webhook_for_shop(
        request: Request,
        session: AsyncSession,
        *,
        shop_owner_id: int | None,
    ):
        from app.services.payment_settlement import (
            handle_card_auto_webhook,
            notify_after_settlement,
        )

        from app.services.security_policy import (
            CARD_WEBHOOK_MAX_BODY_BYTES,
            content_length_ok,
        )

        # Phase 4: reject oversized payloads before buffering the body.
        if not content_length_ok(
            request.headers.get("content-length"), CARD_WEBHOOK_MAX_BODY_BYTES
        ):
            return JSONResponse({"ok": False}, status_code=413)
        body = await request.body()
        if len(body) > CARD_WEBHOOK_MAX_BODY_BYTES:
            return JSONResponse({"ok": False}, status_code=413)
        try:
            settlement = await handle_card_auto_webhook(
                session,
                body=body,
                signature=_sig_from_headers(request),
                shop_owner_id=shop_owner_id,
            )
        except ValueError as exc:
            logger.info("card-auto webhook rejected shop=%s: %s", shop_owner_id, exc)
            # Generic client error — do not echo internal reason to callers.
            return JSONResponse({"ok": False}, status_code=400)
        payment = await session.get(Payment, int(settlement.payment_id))
        order = None
        if payment and payment.order_id:
            order = await session.get(Order, payment.order_id)
        if payment and settlement.status == SettlementStatus.SETTLED.value:
            try:
                await notify_after_settlement(session, payment, order)
            except Exception:
                logger.exception(
                    "card-auto notify failed payment=%s", getattr(payment, "id", None)
                )
        return JSONResponse(
            {
                "ok": True,
                "settlement_id": int(settlement.id),
                "status": settlement.status,
            }
        )

    @app.post("/payments/settlement/card-auto/platform/webhook")
    async def card_auto_webhook_platform(
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        return await _card_auto_webhook_for_shop(
            request, session, shop_owner_id=None
        )

    @app.post("/payments/settlement/card-auto/shop/{reseller_id}/webhook")
    async def card_auto_webhook_shop(
        reseller_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        from app.db.models import BotUser, Role

        if reseller_id <= 0:
            return JSONResponse({"ok": False}, status_code=404)
        owner = await session.get(BotUser, int(reseller_id))
        if not owner or owner.role != Role.RESELLER.value:
            return JSONResponse({"ok": False}, status_code=404)
        return await _card_auto_webhook_for_shop(
            request, session, shop_owner_id=int(reseller_id)
        )

    # Legacy path removed — do not accept unsigned/tenant-ambiguous webhooks.
    @app.api_route(
        "/payments/settlement/card-auto/webhook",
        methods=["GET", "POST"],
    )
    async def card_auto_webhook_legacy_removed():
        return JSONResponse(
            {"ok": False, "error": "use /card-auto/platform/webhook or /card-auto/shop/{id}/webhook"},
            status_code=410,
        )
