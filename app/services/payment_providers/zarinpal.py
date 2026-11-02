"""Zarinpal adapter — protocol-correct; live calls only when merchant_id is set.

Without credentials the adapter refuses create_checkout (fail-closed).
Tests use MockPspAdapter instead of hitting the network.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.payment_providers.base import CheckoutRequest, CheckoutResult, VerifyResult

logger = logging.getLogger(__name__)

_REQUEST_URL = "https://payment.zarinpal.com/pg/v4/payment/request.json"
_VERIFY_URL = "https://payment.zarinpal.com/pg/v4/payment/verify.json"
_START_PAY = "https://www.zarinpal.com/pg/StartPay/{authority}"
_SANDBOX_REQUEST = "https://sandbox.zarinpal.com/pg/v4/payment/request.json"
_SANDBOX_VERIFY = "https://sandbox.zarinpal.com/pg/v4/payment/verify.json"
_SANDBOX_START = "https://sandbox.zarinpal.com/pg/StartPay/{authority}"


class ZarinpalAdapter:
    name = "zarinpal"

    def __init__(self, *, merchant_id: str, sandbox: bool = True):
        self.merchant_id = (merchant_id or "").strip()
        self.sandbox = bool(sandbox)

    def _urls(self) -> tuple[str, str, str]:
        if self.sandbox:
            return _SANDBOX_REQUEST, _SANDBOX_VERIFY, _SANDBOX_START
        return _REQUEST_URL, _VERIFY_URL, _START_PAY

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        if not self.merchant_id or self.merchant_id in {"", "changeme", "your-merchant-id"}:
            raise ValueError("مرچنت زرین‌پال تنظیم نشده")
        request_url, _, start_tpl = self._urls()
        # Zarinpal expects amount in Rials for some APIs; panel stores Toman.
        # Contract: we send Toman×10 as Rials (IRT display uses Toman in UI).
        amount_rial = int(req.amount) * 10
        payload = {
            "merchant_id": self.merchant_id,
            "amount": amount_rial,
            "callback_url": req.callback_url,
            "description": (req.description or f"payment #{req.payment_id}")[:255],
            "metadata": {
                "payment_id": str(req.payment_id),
                "settlement_id": str(req.settlement_id),
            },
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(request_url, json=payload)
            data = resp.json() if resp.content else {}
        errors = data.get("errors")
        result = data.get("data") or {}
        authority = str(result.get("authority") or "").strip()
        code = result.get("code")
        if not authority or code not in {100, "100"}:
            msg = ""
            if isinstance(errors, dict):
                msg = str(errors.get("message") or errors)
            elif errors:
                msg = str(errors)
            raise ValueError(msg or "ساخت تراکنش زرین‌پال ناموفق بود")
        return CheckoutResult(
            external_ref=authority,
            checkout_url=start_tpl.format(authority=authority),
            raw=data if isinstance(data, dict) else None,
        )

    async def verify(
        self,
        *,
        external_ref: str,
        amount: int,
        callback_params: dict[str, Any],
        expected_invoice_id: int | None = None,
    ) -> VerifyResult:
        if not self.merchant_id:
            return VerifyResult(
                ok=False,
                external_ref=external_ref,
                message="مرچنت زرین‌پال تنظیم نشده",
            )
        status = str(callback_params.get("Status") or callback_params.get("status") or "")
        if status.upper() != "OK":
            return VerifyResult(
                ok=False,
                external_ref=external_ref,
                message="پرداخت توسط کاربر تکمیل نشد",
            )
        _, verify_url, _ = self._urls()
        amount_rial = int(amount) * 10
        payload = {
            "merchant_id": self.merchant_id,
            "amount": amount_rial,
            "authority": external_ref,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(verify_url, json=payload)
                data = resp.json() if resp.content else {}
        except Exception as exc:
            logger.warning("zarinpal verify network error: %s", exc)
            return VerifyResult(
                ok=False,
                external_ref=external_ref,
                message="خطا در ارتباط با درگاه",
            )
        result = data.get("data") or {}
        code = result.get("code")
        # 100 = first verify success; 101 = already verified
        if code not in {100, 101, "100", "101"}:
            return VerifyResult(
                ok=False,
                external_ref=external_ref,
                message=str((data.get("errors") or result.get("message") or "verify failed")),
                raw=data if isinstance(data, dict) else None,
            )
        return VerifyResult(
            ok=True,
            external_ref=external_ref,
            amount=int(amount),
            message="verified",
            raw=data if isinstance(data, dict) else None,
        )
