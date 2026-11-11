"""«اشتراک من» › کیف پول، اعتبار و صورت‌حساب، هزینه‌ها: the shop owner's own money in the web panel.

Thin routes over the services the bot and the Mini App already use (``wallet_topup``,
``payment_settlement``, ``shop_credit``, ``credit_statements``, ``payg_charges``). The
owner comes from the session (``shop_owner_context``), never from a form; a top-up goes to the
owner's platform purse, where PAYG charges and credit live, with the platform's own
payment methods and reviewers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.reseller_subscription_pages import shop_owner_context
from app.db.models import CreditStatement, ResellerCharge
from app.services.billing import is_payg
from app.services.numbers import parse_int
from app.services.payment_settlement import check_psp_payment, claim_psp_check_slot
from app.services.redact import user_safe_error
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user
from app.services.shop_credit import credit_overview
from app.services.users import get_all_settings
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

WALLET = "/my-subscription/wallet"
CREDIT = "/my-subscription/credit"
CHARGES = "/my-subscription/charges"
CHARGES_PAGE_SIZE = 50
STATEMENTS_SHOWN = 24
_ID_MAX = 2**31 - 1


def _back(path: str, *, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"err={quote(err)}" if err else f"ok={quote(ok)}"
    return RedirectResponse(f"{path}?{query}", status_code=303)


def _flash(request: Request) -> dict[str, Any]:
    return {"flash_ok": request.query_params.get("ok"), "flash_err": request.query_params.get("err")}


def _amount(raw: str) -> int | None:
    try:
        return parse_int(str(raw or "").strip())
    except ValueError:
        return None


def _positive_id(raw: object) -> int | None:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= _ID_MAX else None


async def _wallet_ctx(session: AsyncSession, owner, profile) -> dict[str, Any]:
    ui = await get_all_settings(session)
    return {
        "tab": "wallet", "owner": owner, "is_payg": is_payg(profile),
        "options": topup_options(ui), "recent": await recent_topups(session, int(owner.id), shop_id=None),
        "credit": await credit_overview(session, int(owner.id), balance=int(owner.wallet_balance or 0)),
    }


async def _charges_page(session: AsyncSession, shop_id: int, before: int | None) -> list[ResellerCharge]:
    """Newest first, keyset-paged by id."""
    query = select(ResellerCharge).where(ResellerCharge.shop_id == int(shop_id))
    if before is not None:
        query = query.where(ResellerCharge.id < int(before))
    return list((await session.scalars(query.order_by(ResellerCharge.id.desc()).limit(CHARGES_PAGE_SIZE))).all())


async def _submit_receipt(session: AsyncSession, owner, payment_id: int | None, image: bytes) -> RedirectResponse:
    if payment_id is None or not image:
        return _back(WALLET, err="رسید نامعتبر است")
    if len(image) > RECEIPT_MAX_BYTES:
        return _back(WALLET, err="حجم تصویر رسید باید کمتر از ۸ مگابایت باشد")
    try:
        bot, should_close = await open_notify_bot_for_user(session, owner, shop_id=None)
    except ShopBotUnavailable:
        logger.warning("web top-up receipt: platform bot unavailable", exc_info=True)
        return _back(WALLET, err="ربات در دسترس نیست؛ کمی بعد دوباره تلاش کنید")
    try:
        result = await submit_topup_receipt(session, bot=bot, user_id=int(owner.id), shop_id=None,
                                            telegram_id=int(owner.telegram_id), payment_id=payment_id, image=image)
    except TopupError as exc:
        return _back(WALLET, err=str(exc))
    finally:
        if should_close:
            await bot.session.close()
    return _back(WALLET, ok=result.message)


async def _check_gateway(session: AsyncSession, owner, settlement_id: int | None) -> RedirectResponse:
    if settlement_id is None:
        return _back(WALLET, err="پرداخت نامعتبر است")
    if not claim_psp_check_slot(settlement_id):
        return _back(WALLET, err="چند ثانیه بعد دوباره بزنید")
    try:
        outcome = await check_psp_payment(session, settlement_id=settlement_id, user_id=int(owner.id), shop_id=None)
    except ValueError as exc:
        return _back(WALLET, err=user_safe_error(exc, fallback="بررسی پرداخت ناموفق بود"))
    return _back(WALLET, ok=PSP_CHECK_MESSAGES[outcome])


def register_reseller_finance_pages(app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable) -> None:
    @app.get(WALLET, response_class=HTMLResponse)
    async def my_wallet(request: Request, staff: dict = Depends(require_staff),
                        session: AsyncSession = Depends(get_db)) -> Any:
        owner, profile = await shop_owner_context(session, staff)
        return render(request, "my_finance.html", {"staff": staff, **await _wallet_ctx(session, owner, profile),
                                                   **_flash(request)})

    @app.post(f"{WALLET}/topup", response_class=HTMLResponse)
    async def my_wallet_topup(request: Request, amount: str = Form(""), method: str = Form(""),
                              dest_id: str = Form(""), staff: dict = Depends(require_staff),
                              session: AsyncSession = Depends(get_db)) -> Any:
        owner, profile = await shop_owner_context(session, staff)
        try:
            start = await start_topup(session, user_id=int(owner.id), shop_id=None, amount=_amount(amount),
                                      method=method, dest_id=dest_id or None, ui=await get_all_settings(session))
        except TopupError as exc:
            return _back(WALLET, err=str(exc))
        except ValueError as exc:
            logger.warning("web top-up start refused owner=%s", owner.id, exc_info=True)
            return _back(WALLET, err=user_safe_error(exc, fallback="شروع پرداخت ناموفق بود"))
        # The payment instructions are shown once, on this response; the payment stays in «recent».
        return render(request, "my_finance.html", {"staff": staff, **await _wallet_ctx(session, owner, profile),
                                                   "topup": start.payload()})

    @app.post(WALLET + "/topup/{payment_id}/receipt")
    async def my_wallet_receipt(payment_id: int, receipt: UploadFile = File(...),
                                staff: dict = Depends(require_staff),
                                session: AsyncSession = Depends(get_db)) -> RedirectResponse:
        owner, _profile = await shop_owner_context(session, staff)
        image = await receipt.read(RECEIPT_MAX_BYTES + 1)
        return await _submit_receipt(session, owner, _positive_id(payment_id), image)

    @app.post(f"{WALLET}/check")
    async def my_wallet_check(settlement_id: str = Form(""), staff: dict = Depends(require_staff),
                              session: AsyncSession = Depends(get_db)) -> RedirectResponse:
        owner, _profile = await shop_owner_context(session, staff)
        return await _check_gateway(session, owner, _positive_id(settlement_id))

    @app.get(CREDIT, response_class=HTMLResponse)
    async def my_credit(request: Request, staff: dict = Depends(require_staff),
                        session: AsyncSession = Depends(get_db)) -> Any:
        owner, profile = await shop_owner_context(session, staff)
        statements = (await session.scalars(
            select(CreditStatement).where(CreditStatement.shop_id == int(owner.id))
            .order_by(CreditStatement.period_start.desc()).limit(STATEMENTS_SHOWN)
        )).all()
        return render(request, "my_finance.html", {
            "staff": staff, "tab": "credit", "owner": owner, "is_payg": is_payg(profile),
            "credit": await credit_overview(session, int(owner.id), balance=int(owner.wallet_balance or 0)),
            "statements": statements, **_flash(request),
        })

    @app.get(CHARGES, response_class=HTMLResponse)
    async def my_charges(request: Request, staff: dict = Depends(require_staff),
                         session: AsyncSession = Depends(get_db)) -> Any:
        owner, profile = await shop_owner_context(session, staff)
        before = _positive_id(request.query_params.get("before"))
        charges = await _charges_page(session, int(owner.id), before)
        next_before = charges[-1].id if len(charges) == CHARGES_PAGE_SIZE else None
        return render(request, "my_finance.html", {
            "staff": staff, "tab": "charges", "owner": owner, "is_payg": is_payg(profile),
            "charges": charges, "next_before": next_before, **_flash(request),
        })
