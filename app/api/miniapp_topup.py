"""Mini App wallet top-up: options, start a payment, upload its receipt, check a gateway.

Thin routes over ``app.services.wallet_topup``. The customer comes from initData and
the purse from the verified Mini App scope: a shop's Mini App tops up that shop's purse
with that shop's own cards and gateways, and its receipts go through that shop's bot to
its reviewers. Every payment id is re-checked against the customer and purse in the service.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store, _require_commerce_ready, _scope_ui
from app.services.miniapp_auth import load_mini_user
from app.services.miniapp_shop import mini_scope
from app.services.payment_settlement import check_psp_payment, claim_psp_check_slot
from app.services.redact import user_safe_error
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user
from app.services.security_policy import content_length_ok
from app.services.wallet_topup import (
    PSP_CHECK_MESSAGES,
    RECEIPT_MAX_BYTES,
    TopupError,
    recent_topups,
    start_topup,
    submit_topup_receipt,
    topup_options,
)

logger = logging.getLogger(__name__)

_TOO_LARGE = "حجم تصویر رسید باید کمتر از ۸ مگابایت باشد"
_ID_MAX = 2**31 - 1


def _valid_id(value: object) -> int:
    if type(value) is not int or not 0 < value <= _ID_MAX:
        raise HTTPException(400, "شناسهٔ پرداخت نامعتبر است")
    return value


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the raw body, stopping as soon as it grows past ``limit``."""
    if not content_length_ok(request.headers.get("content-length"), limit):
        raise HTTPException(413, _TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, _TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "درخواست نامعتبر است") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "درخواست نامعتبر است")
    return body


def register_miniapp_topup(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/topup")
    async def mini_topup_options(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        ui = await _scope_ui(session, request)
        recent = await recent_topups(session, int(user.id), shop_id=mini_scope(request).shop_id)
        return _no_store({**topup_options(ui), "recent": recent})

    @app.post("/api/mini/topup")
    async def mini_topup_start(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        user_id = int(user.id)
        body = await _json_object(request)
        ui = await _scope_ui(session, request)
        try:
            start = await start_topup(
                session, user_id=user_id, shop_id=mini_scope(request).shop_id, amount=body.get("amount"),
                method=body.get("method"), dest_id=body.get("dest_id"), ui=ui,
            )
        except TopupError as exc:
            raise HTTPException(400, str(exc)) from None
        except ValueError as exc:
            logger.warning("mini top-up start refused user=%s", user_id, exc_info=True)
            raise HTTPException(400, user_safe_error(exc, fallback="شروع پرداخت ناموفق بود")) from None
        return _no_store({"topup": start.payload()})

    @app.post("/api/mini/topup/{payment_id}/receipt")
    async def mini_topup_receipt(payment_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        payment_id = _valid_id(payment_id)
        image = await _read_capped(request, RECEIPT_MAX_BYTES)
        shop_id = mini_scope(request).shop_id
        try:
            bot, should_close = await open_notify_bot_for_user(session, user, shop_id=shop_id)
        except ShopBotUnavailable:
            logger.warning("mini top-up receipt: shop bot unavailable shop=%s", shop_id, exc_info=True)
            raise HTTPException(503, "ربات فروشگاه در دسترس نیست؛ کمی بعد دوباره تلاش کنید") from None
        try:
            result = await submit_topup_receipt(
                session, bot=bot, user_id=int(user.id), shop_id=shop_id, telegram_id=int(user.telegram_id),
                payment_id=payment_id, image=image,
            )
        except TopupError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            if should_close:
                await bot.session.close()
        return _no_store({"status": result.status, "message": result.message})

    @app.post("/api/mini/topup/check")
    async def mini_topup_check(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        settlement_id = _valid_id((await _json_object(request)).get("settlement_id"))
        if not claim_psp_check_slot(settlement_id):
            raise HTTPException(429, "چند ثانیه بعد دوباره بزنید")
        try:
            outcome = await check_psp_payment(
                session, settlement_id=settlement_id, user_id=int(user.id), shop_id=mini_scope(request).shop_id
            )
        except ValueError as exc:
            raise HTTPException(400, user_safe_error(exc, fallback="بررسی پرداخت ناموفق بود")) from None
        return _no_store({"outcome": outcome.value, "message": PSP_CHECK_MESSAGES[outcome]})
