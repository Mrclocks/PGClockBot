"""Settlement HTTP endpoints — fail-closed, tenant-scoped, mock gated.

Public surface:
  - Checkout go-link — panel-hosted redirect so referrer matches PSP callback host
  - Real PSP return (GET/POST) — verifies via provider API
  - Card-auto webhooks — per-tenant path + HMAC secret from that tenant only
  - BezanBeCard callback/return — tenant path + checkout token; always re-fetches API
  - Mock checkout/pay — ONLY when ALLOW_SETTLEMENT_MOCK=1 + one-time token
"""

from __future__ import annotations

import logging
import secrets
from html import escape
from typing import Any, Callable

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SettlementStatus

logger = logging.getLogger(__name__)

_BOT_RETURN_LABEL = "بازگشت به ربات"
_OPEN_SETTLEMENT_STATUSES = frozenset(
    {
        SettlementStatus.CREATED.value,
        SettlementStatus.AWAITING.value,
        SettlementStatus.SETTLING.value,
    }
)

# Browser tab titles matching the original simple payment-return pages.
_BROWSER_TITLE = {
    "ok": "موفق",
    "pending": "در انتظار",
    "warn": "توجه",
    "error": "ناموفق",
}


async def _bot_return_href(
    session: AsyncSession | None,
    shop_owner_id: int | None,
) -> str:
    """``tg://resolve?domain=…`` (or https t.me fallback) for the return CTA."""
    from app.services.ux20 import bot_deep_link, bot_tg_resolve_link, shop_bot_chat_url

    if session is not None:
        return (await shop_bot_chat_url(session, shop_owner_id=shop_owner_id)) or ""
    from app.config import get_settings

    uname = get_settings().bot_username
    return bot_tg_resolve_link(uname) or bot_deep_link(uname, "") or ""


def _simple_payment_page(
    *,
    browser_title: str,
    heading: str,
    message: str,
    ok: bool = True,
    status_code: int = 200,
    body_html: str = "",
    bot_href: str = "",
) -> HTMLResponse:
    """Compact card UI that used to complete the Paystar return happily."""
    color = "#16a34a" if ok else "#dc2626"
    bot_block = ""
    if bot_href:
        bot_block = (
            f'<p><a class="btn" href="{escape(bot_href, quote=True)}">'
            f"{escape(_BOT_RETURN_LABEL)}</a></p>"
        )
    extra = body_html or ""
    doc = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{escape(browser_title)}</title>
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
<body><div class="card">
<h1>{escape(heading)}</h1>
<p>{escape(message)}</p>
{extra}
{bot_block}
</div></body>
</html>"""
    return HTMLResponse(doc, status_code=status_code)


async def _status(
    request: Request,
    session: AsyncSession | None = None,
    *,
    shop_owner_id: int | None = None,
    kind: str,
    title: str,
    message: str,
    http_status: int = 200,
    watermark: str | None = None,
    body_html: str = "",
):
    del request, watermark  # kept for call-site compatibility
    bot_href = await _bot_return_href(session, shop_owner_id)
    ok = kind in {"ok", "200"}
    browser_title = _BROWSER_TITLE.get(kind, title)
    return _simple_payment_page(
        browser_title=browser_title,
        heading=title,
        message=message,
        ok=ok,
        status_code=http_status,
        body_html=body_html,
        bot_href=bot_href,
    )


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


async def _request_callback_params(request: Request) -> dict[str, str]:
    """Merge query string and POST form/json body (Paystar often POSTs the return)."""
    params: dict[str, str] = {
        str(k): str(v) for k, v in request.query_params.multi_items()
    }
    if request.method.upper() != "POST":
        return params
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            for key, value in body.items():
                if value is not None:
                    params[str(key)] = str(value)
        return params
    try:
        form = await request.form()
    except Exception:
        return params
    for key, value in form.multi_items():
        params[str(key)] = str(value)
    return params


def _register_checkout_go(app: Any, get_db: Callable[..., Any]) -> None:
    @app.get("/payments/settlement/go/{settlement_id}")
    async def settlement_checkout_go(
        settlement_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        """302 to checkout after token check — keeps referrer on the panel host."""
        from app.db.models import PaymentSettlement

        token = (request.query_params.get("t") or "").strip()
        settlement = await session.get(PaymentSettlement, settlement_id)
        expected = (settlement.checkout_token if settlement else "") or ""
        if (
            not settlement
            or not token
            or not expected
            or not secrets.compare_digest(token, expected)
        ):
            return await _status(
                request,
                session,
                kind="error",
                title="لینک نامعتبر است",
                message="به ربات برگردید و دوباره پرداخت را شروع کنید.",
                http_status=404,
                watermark="404",
            )
        if settlement.status == SettlementStatus.SETTLED.value:
            return await _status(
                request,
                session,
                shop_owner_id=settlement.shop_owner_id,
                kind="ok",
                title="قبلاً تأیید شده",
                message="می‌توانید به تلگرام برگردید.",
                watermark="OK",
            )
        target = (settlement.checkout_url or "").strip()
        if not target.startswith("https://"):
            return await _status(
                request,
                session,
                shop_owner_id=settlement.shop_owner_id,
                kind="error",
                title="لینک پرداخت آماده نیست",
                message="به ربات برگردید و دوباره تلاش کنید.",
                http_status=400,
                watermark="ERR",
            )
        return RedirectResponse(url=target, status_code=302)


def _bbc_return_page(settlement: Any) -> tuple[str, str, str]:
    """What the payer sees after the payment page; never internal ids or reasons."""
    status = settlement.status
    error = str(settlement.error_message or "")
    if status == SettlementStatus.SETTLED.value:
        return "ok", "پرداخت تأیید شد", "سرویس در تلگرام برای شما فرستاده شد."
    if error.startswith("review:") or status == SettlementStatus.FAILED.value:
        return (
            "warn",
            "پرداخت در حال بررسی است",
            "فروشگاه واریز را بررسی می‌کند و نتیجه در ربات به شما اطلاع داده می‌شود.",
        )
    if status == SettlementStatus.EXPIRED.value:
        return (
            "error",
            "مهلت پرداخت تمام شد",
            "به ربات برگردید و دوباره پرداخت را شروع کنید.",
        )
    return (
        "pending",
        "هنوز واریزی ثبت نشده",
        "بعد از واریز دقیقِ مبلغ یکتا، تأیید و تحویل خودکار انجام می‌شود.",
    )


def _register_mock_psp(app: Any, get_db: Callable[..., Any]) -> None:
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
            return await _status(
                request,
                session,
                kind="error",
                title="درخواست نامعتبر",
                message="شناسه تسویه درست نیست.",
                http_status=400,
            )
        settlement = await session.get(PaymentSettlement, settlement_id)
        if (
            not settlement
            or settlement.channel != CHANNEL_PSP
            or settlement.provider != "mock"
        ):
            return await _status(
                request,
                session,
                kind="error",
                title="درخواست نامعتبر",
                message="این صفحه درگاه آزمایشی یافت نشد.",
                http_status=404,
                watermark="404",
            )
        from app.services.payment_settlement import _require_mock_token

        shop_id = settlement.shop_owner_id
        try:
            _require_mock_token(settlement, token)
        except ValueError:
            return await _status(
                request,
                session,
                shop_owner_id=shop_id,
                kind="error",
                title="درخواست نامعتبر",
                message="توکن این درگاه آزمایشی درست نیست.",
                http_status=403,
                watermark="403",
            )

        if settlement.status == SettlementStatus.SETTLED.value:
            return await _status(
                request,
                session,
                shop_owner_id=shop_id,
                kind="ok",
                title="قبلاً تسویه شده",
                message="این پرداخت پیش‌تر تأیید شده است.",
                watermark="OK",
            )

        amount = int(settlement.amount)
        body = f"""
<p>مبلغ: <b dir="ltr">{amount:,}</b> تومان</p>
<form method="post" action="/payments/settlement/mock/pay">
  <input type="hidden" name="settlement_id" value="{int(settlement.id)}"/>
  <input type="hidden" name="amount" value="{amount}"/>
  <input type="hidden" name="token" value="{escape(token)}"/>
  <button class="btn" type="submit">پرداخت آزمایشی موفق</button>
</form>
<p class="muted">فقط با ALLOW_SETTLEMENT_MOCK=1</p>
"""
        return await _status(
            request,
            session,
            shop_owner_id=shop_id,
            kind="pending",
            title="درگاه آزمایشی (Mock)",
            message="برای ادامه دکمهٔ زیر را بزنید.",
            watermark="MOCK",
            body_html=body,
        )

    @app.post("/payments/settlement/mock/pay", response_class=HTMLResponse)
    async def mock_pay(
        request: Request,
        settlement_id: int = Form(...),
        amount: int = Form(...),
        token: str = Form(...),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.payment_settlement import (
            deliver_psp_once,
            mock_psp_pay,
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
            return await _status(
                request,
                session,
                kind="error",
                title="تسویه ناموفق",
                message="درخواست رد شد.",
                http_status=400,
            )
        if settlement.status == SettlementStatus.SETTLED.value:
            try:
                await deliver_psp_once(session, settlement)
            except Exception:
                logger.exception("mock pay notify failed settlement=%s", settlement.id)
        return await _status(
            request,
            session,
            shop_owner_id=settlement.shop_owner_id,
            kind="ok",
            title="پرداخت آزمایشی تأیید شد",
            message="می‌توانید به تلگرام برگردید.",
            watermark="OK",
        )


async def _psp_return_unverified_page(
    request: Request,
    session: AsyncSession,
    settlement_id: int,
    params: dict[str, str],
) -> HTMLResponse:
    """The payment was not confirmed on this visit: say why without alarming the payer.

    A settlement that is still open is not a failure: the customer cancelled at the
    bank, or the gateway has not reported the payment yet and the bot keeps checking.
    """
    from app.db.models import PaymentSettlement
    from app.services.db_safe import rollback_quiet
    from app.services.payment_settlement import callback_indicates_user_abort

    # Reload after the failed verify so the page reflects what was committed.
    await rollback_quiet(session)
    settlement = await session.get(PaymentSettlement, settlement_id)
    shop_owner_id = settlement.shop_owner_id if settlement else None
    if settlement is None or settlement.status not in _OPEN_SETTLEMENT_STATUSES:
        return await _status(
            request,
            session,
            shop_owner_id=shop_owner_id,
            kind="error",
            title="تأیید پرداخت ناموفق",
            message="اگر مبلغ کم شده با پشتیبانی تماس بگیرید.",
            http_status=400,
        )
    if callback_indicates_user_abort(params):
        return await _status(
            request,
            session,
            shop_owner_id=shop_owner_id,
            kind="warn",
            title="پرداخت انجام نشد",
            message=(
                "پرداخت در درگاه تکمیل نشد. اگر مبلغی کسر شده باشد، بانک طبق روال خود آن را برمی‌گرداند. "
                "به ربات برگردید و در صورت تمایل دوباره پرداخت کنید."
            ),
        )
    return await _status(
        request,
        session,
        shop_owner_id=shop_owner_id,
        kind="pending",
        title="پرداخت هنوز تأیید نشده",
        message="اگر پرداخت را کامل کرده‌اید به ربات برگردید؛ تأیید و تحویل خودکار انجام می‌شود.",
    )


def _register_psp_return(app: Any, get_db: Callable[..., Any]) -> None:
    @app.api_route(
        "/payments/settlement/psp/{provider}/return/{settlement_id}",
        methods=["GET", "POST"],
    )
    async def psp_return(
        provider: str,
        settlement_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        from app.db.models import PaymentSettlement
        from app.services.payment_settlement import (
            complete_psp_return,
            deliver_psp_once,
            settlement_mock_allowed,
        )
        from app.services.psp_settlement_review import is_in_review

        provider_l = (provider or "").strip().lower()
        if provider_l == "mock" and not settlement_mock_allowed():
            return Response(status_code=404, content="Not Found")

        params = await _request_callback_params(request)
        pre = await session.get(PaymentSettlement, settlement_id)
        if not pre:
            return await _status(
                request,
                session,
                kind="error",
                title="تسویه یافت نشد",
                message="اگر مبلغ کم شده با پشتیبانی تماس بگیرید.",
                http_status=404,
                watermark="404",
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
            return await _psp_return_unverified_page(request, session, settlement_id, params)
        if settlement.status == SettlementStatus.SETTLED.value:
            try:
                await deliver_psp_once(session, settlement)
            except Exception:
                logger.exception("psp return notify failed settlement=%s", settlement.id)
            return await _status(
                request,
                session,
                shop_owner_id=settlement.shop_owner_id,
                kind="ok",
                title="پرداخت تأیید شد",
                message="می‌توانید به تلگرام برگردید.",
                watermark="OK",
            )
        if settlement.status == SettlementStatus.AWAITING.value:
            return await _status(
                request,
                session,
                shop_owner_id=settlement.shop_owner_id,
                kind="pending",
                title="پرداخت هنوز تأیید نشده",
                message="اگر پرداخت را کامل کردید کمی صبر کنید یا به ربات برگردید.",
                watermark="…",
            )
        if is_in_review(settlement):
            return await _status(
                request,
                session,
                shop_owner_id=settlement.shop_owner_id,
                kind="warn",
                title="پرداخت در حال بررسی است",
                message="پرداخت شما ثبت شد و فروشگاه آن را بررسی می‌کند؛ نتیجه در ربات اطلاع داده می‌شود.",
                watermark="!",
            )
        return await _status(
            request,
            session,
            shop_owner_id=settlement.shop_owner_id,
            kind="warn",
            title="پرداخت تکمیل نشد",
            message="می‌توانید به ربات برگردید و دوباره تلاش کنید.",
            watermark="!",
        )


def _register_card_auto(app: Any, get_db: Callable[..., Any]) -> None:
    async def _card_auto_webhook_for_shop(
        request: Request,
        session: AsyncSession,
        *,
        shop_owner_id: int | None,
    ):
        from app.db.models import Order, Payment
        from app.services.payment_settlement import (
            handle_card_auto_webhook,
            notify_after_settlement,
        )

        body = await request.body()
        if len(body) > 64 * 1024:
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
        return await _card_auto_webhook_for_shop(request, session, shop_owner_id=None)

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

    @app.api_route(
        "/payments/settlement/card-auto/webhook",
        methods=["GET", "POST"],
    )
    async def card_auto_webhook_legacy_removed():
        return JSONResponse(
            {
                "ok": False,
                "error": "use /card-auto/platform/webhook or /card-auto/shop/{id}/webhook",
            },
            status_code=410,
        )


def _register_bbc(app: Any, get_db: Callable[..., Any]) -> None:
    async def _bbc_finish(
        request: Request,
        session: AsyncSession,
        *,
        tenant_key: int,
        settlement_id: int,
        token: str,
        as_html: bool,
    ):
        from app.services.bbc_settlement import handle_bbc_link

        try:
            settlement = await handle_bbc_link(
                session, settlement_id=settlement_id, tenant_key=tenant_key, token=token
            )
        except ValueError:
            logger.info("bbc link rejected tenant=%s settlement=%s", tenant_key, settlement_id)
            if as_html:
                shop_id = int(tenant_key) if int(tenant_key) > 0 else None
                return await _status(
                    request,
                    session,
                    shop_owner_id=shop_id,
                    kind="error",
                    title="این لینک پرداخت معتبر نیست",
                    message="به ربات برگردید و وضعیت سفارش را آنجا ببینید.",
                    http_status=404,
                    watermark="404",
                )
            return JSONResponse({"ok": False}, status_code=404)
        if not as_html:
            return JSONResponse({"ok": True})
        kind, title, message = _bbc_return_page(settlement)
        return await _status(
            request,
            session,
            shop_owner_id=settlement.shop_owner_id,
            kind=kind,
            title=title,
            message=message,
            watermark="OK" if kind == "ok" else kind.upper()[:4],
        )

    @app.api_route(
        "/payments/settlement/bbc/tenant/{tenant_key}/callback/{settlement_id}",
        methods=["GET", "POST"],
    )
    async def bbc_callback(
        tenant_key: int,
        settlement_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        token = (request.query_params.get("t") or "").strip()
        return await _bbc_finish(
            request,
            session,
            tenant_key=tenant_key,
            settlement_id=settlement_id,
            token=token,
            as_html=False,
        )

    @app.get("/payments/settlement/bbc/tenant/{tenant_key}/return/{settlement_id}")
    async def bbc_return(
        tenant_key: int,
        settlement_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
    ):
        token = (request.query_params.get("t") or "").strip()
        return await _bbc_finish(
            request,
            session,
            tenant_key=tenant_key,
            settlement_id=settlement_id,
            token=token,
            as_html=True,
        )


def register_settlement_pages(app, *, get_db):
    _register_checkout_go(app, get_db)
    _register_mock_psp(app, get_db)
    _register_psp_return(app, get_db)
    _register_card_auto(app, get_db)
    _register_bbc(app, get_db)
