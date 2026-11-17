"""TonPays PSP adapter — request → redirect → webhook/callback → verify (amounts in Toman).

Follows the TonPays merchant API:
  POST https://tonpays.online/api/v1/invoices/create  JSON body → invoice_id, invoice_url
  GET  https://t.me/TonPaysInvoiceBot?start=inv_{invoice_id}  or  https://pay.tonpays.online/i/{invoice_id}  browser checkout
  GET  https://tonpays.online/api/v1/invoices/check/{invoice_id}  read-only verify
  POST https://tonpays.online/api/v1/invoices/check  body {invoice_id} → amount/reference

Webhook: POST to callback_url with X-TonPays-Signature (t=<unix>,v1=<hmac_sha256_hex> on timestamp + "." + raw_body)

Verify never cancels or refunds; callers must match amount and invoice_id locally.
Unknown / transport failures must leave the settlement open for retry.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.payment_providers.base import CheckoutRequest, CheckoutResult, VerifyResult

logger = logging.getLogger(__name__)

_MIN_AMOUNT_TOMAN = 1000
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_API_BASE = "https://tonpays.online"
_CREATE_URL = f"{_API_BASE}/api/v1/invoices/create"
_CHECK_URL = f"{_API_BASE}/api/v1/invoices/check"
_INVOICE_URL_TPL = "https://t.me/TonPaysInvoiceBot?start=inv_{invoice_id}"
_WEB_INVOICE_URL_TPL = "https://pay.tonpays.online/i/{invoice_id}"

# Invoice statuses from TonPays docs
_STATUS_PENDING = "pending"
_STATUS_PROCESSING = "processing"
_STATUS_COMPLETED = "completed"
_STATUS_REJECTED = "rejected"
_STATUS_EXPIRED = "expired"
_STATUS_CANCELED = "canceled"
_STATUS_ACTION_LESS = "action_required_less"
_STATUS_ACTION_MORE = "action_required_more"

_COMPLETED_STATUSES = frozenset({_STATUS_COMPLETED})
_FAILED_STATUSES = frozenset({_STATUS_REJECTED, _STATUS_EXPIRED, _STATUS_CANCELED})
_OPEN_STATUSES = frozenset({_STATUS_PENDING, _STATUS_PROCESSING, _STATUS_ACTION_LESS, _STATUS_ACTION_MORE})


class TonPaysError(ValueError):
    """Customer-safe message with optional admin/log detail."""

    def __init__(self, customer: str, *, admin_detail: str = "") -> None:
        super().__init__(customer)
        self.admin_detail = (admin_detail or customer).strip()


def _safe_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} نامعتبر است")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} باید عدد صحیح باشد")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} نامعتبر است") from exc
    return number


def _validate_callback_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("آدرس وب‌هوک تنظیم نشده")
    try:
        parts = urlparse(raw)
    except ValueError as exc:
        raise ValueError("آدرس وب‌هوک نامعتبر است") from exc
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("آدرس وب‌هوک باید با https و میزبان عمومی باشد")
    return raw


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Verify X-TonPays-Signature: t=<unix>,v1=<hmac_sha256_hex> on timestamp + '.' + raw_body."""
    if not secret or not signature:
        return False
    # Parse t=<unix>,v1=<hmac>
    m = re.match(r"t=(\d+),v1=([0-9a-fA-F]+)", signature.strip())
    if not m:
        return False
    timestamp = m.group(1)
    expected_hmac = m.group(2).lower()
    # Compute HMAC-SHA256 on "{timestamp}.{raw_body}"
    message = f"{timestamp}.".encode() + body
    computed = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest().lower()
    return hmac.compare_digest(computed, expected_hmac)


class TonPaysAdapter:
    name = "tonpays"

    def __init__(self, *, api_key: str, sandbox: bool = False):
        self.api_key = (api_key or "").strip()
        self.sandbox = bool(sandbox)
        # TonPays doesn't have a sandbox environment; ignore sandbox flag but keep for interface compatibility

    def _require_config(self) -> None:
        if not self.api_key:
            raise ValueError("کلید API تون پیز تنظیم نشده")

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        self._require_config()
        amount = _safe_int(req.amount, field="مبلغ")
        if amount < _MIN_AMOUNT_TOMAN:
            raise ValueError(f"حداقل مبلغ تون پیز {_MIN_AMOUNT_TOMAN} تومان است")

        settlement_id = _safe_int(req.settlement_id, field="invoice_id")
        if settlement_id <= 0:
            raise ValueError("invoice_id باید بزرگ‌تر از صفر باشد")

        callback_url = _validate_callback_url(req.callback_url)

        payload = {
            "amount": amount,
            "order_id": f"settlement-{settlement_id}",
            "callback_url": callback_url,
        }
        # Optional: buyer_chat_id if we have it from metadata
        buyer_chat_id = req.metadata.get("buyer_chat_id") if req.metadata else None
        if buyer_chat_id:
            try:
                payload["buyer_chat_id"] = int(buyer_chat_id)
            except (TypeError, ValueError):
                pass

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_CREATE_URL, json=payload, headers=self._headers())
            data = resp.json() if resp.content else {}

        if resp.status_code >= 400:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("detail", {}).get("message", "") or data.get("detail", "") or "")
            logger.warning("tonpays create failed status=%s detail=%s", resp.status_code, detail)
            raise TonPaysError(
                "درخواست تون پیز ناموفق بود",
                admin_detail=f"HTTP {resp.status_code}: {detail}",
            )

        if not isinstance(data, dict):
            raise TonPaysError("پاسخ نامعتبر درگاه تون پیز", admin_detail="non-dict response")

        invoice_id = str(data.get("invoice_id") or "").strip()
        if not invoice_id:
            raise TonPaysError("درخواست تون پیز ناموفق بود", admin_detail="missing invoice_id")

        # Prefer web_invoice_url for browser, fallback to Telegram bot deep link
        checkout_url = str(data.get("web_invoice_url") or data.get("invoice_url") or "").strip()
        if not checkout_url:
            checkout_url = _WEB_INVOICE_URL_TPL.format(invoice_id=invoice_id)

        return CheckoutResult(
            external_ref=invoice_id,
            checkout_url=checkout_url,
            raw=data,
        )

    async def verify(
        self,
        *,
        external_ref: str,
        amount: int,
        callback_params: dict[str, Any],
        expected_invoice_id: int | None = None,
    ) -> VerifyResult:
        invoice_id = str(external_ref or "").strip()
        if not invoice_id:
            return VerifyResult(ok=False, external_ref="", message="شناسه فاکتور موجود نیست")

        # If callback has status=completed, we still verify via API (read-only check)
        # TonPays webhook sends full invoice object; we can trust it if signature verifies
        # but the adapter interface expects us to call the verify API.
        # We'll use the check endpoint.

        try:
            expected_amount = _safe_int(amount, field="مبلغ")
        except ValueError as exc:
            return VerifyResult(ok=False, external_ref=invoice_id, message=str(exc))

        self._require_config()

        check_url = f"{_API_BASE}/api/v1/invoices/check/{invoice_id}"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(check_url, headers=self._headers())
            data = resp.json() if resp.content else {}

        if resp.status_code >= 400:
            logger.warning("tonpays verify network error status=%s", resp.status_code)
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message="خطا در ارتباط با درگاه",
            )

        if not isinstance(data, dict):
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message="پاسخ نامعتبر درگاه",
            )

        status = str(data.get("status") or "").strip()
        paid = bool(data.get("paid"))
        paid_amount = data.get("request_amount") or data.get("final_amount")

        try:
            paid_amount_int = _safe_int(paid_amount, field="مبلغ") if paid_amount is not None else None
        except ValueError:
            paid_amount_int = None

        # Verify invoice_id matches if expected_invoice_id provided (our settlement row id)
        if expected_invoice_id is not None:
            # TonPays doesn't echo our invoice_id in check response; we match by external_ref
            pass

        if status in _COMPLETED_STATUSES and paid:
            if paid_amount_int is not None and paid_amount_int != expected_amount:
                logger.warning(
                    "tonpays verify amount mismatch invoice=%s expected=%s paid=%s",
                    invoice_id[:16],
                    expected_amount,
                    paid_amount_int,
                )
                return VerifyResult(
                    ok=False,
                    external_ref=invoice_id,
                    amount=paid_amount_int,
                    message="مبلغ تأییدشده با سفارش هم‌خوانی ندارد",
                    raw=data,
                    reference=str(data.get("invoice_id") or "").strip(),
                    review_reason="amount_mismatch",
                )
            reference = str(data.get("invoice_id") or "").strip()
            return VerifyResult(
                ok=True,
                external_ref=invoice_id,
                amount=paid_amount_int if paid_amount_int is not None else expected_amount,
                message="verified",
                raw=data,
                reference=reference,
                paid_at="",  # TonPays doesn't provide paid_at in check response
            )

        if status in _FAILED_STATUSES:
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message="پرداخت تکمیل نشد یا رد شده",
                raw=data,
            )

        if status in _OPEN_STATUSES:
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message="پرداختی برای این تراکنش در درگاه ثبت نشده است",
                raw=data,
            )

        return VerifyResult(
            ok=False,
            external_ref=invoice_id,
            message=f"وضعیت ناشناخته: {status}",
            raw=data,
        )


async def verify_webhook(
    *,
    api_key: str,
    body: bytes,
    signature: str | None,
    webhook_secret: str | None,
) -> VerifyResult | None:
    """Verify TonPays webhook payload. Returns VerifyResult if paid, None if not actionable."""
    if webhook_secret and signature:
        if not _verify_signature(webhook_secret, body, signature):
            logger.warning("tonpays webhook signature verification failed")
            return VerifyResult(ok=False, external_ref="", message="امضای وب‌هوک نامعتبر")

    try:
        import json
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("tonpays webhook invalid JSON: %s", exc)
        return VerifyResult(ok=False, external_ref="", message="JSON نامعتبر")

    if not isinstance(data, dict):
        return None

    event = str(data.get("event") or "").strip()
    invoice_id = str(data.get("invoice_id") or "").strip()
    paid = bool(data.get("paid"))
    status = str(data.get("status") or "").strip()

    if event != "invoice.completed" or not paid or status != _STATUS_COMPLETED:
        return None

    paid_amount = data.get("credit_amount") or data.get("final_amount") or data.get("request_amount")
    try:
        paid_amount_int = _safe_int(paid_amount, field="مبلغ") if paid_amount is not None else None
    except ValueError:
        paid_amount_int = None

    reference = str(data.get("invoice_id") or "").strip()
    return VerifyResult(
        ok=True,
        external_ref=invoice_id,
        amount=paid_amount_int,
        message="verified via webhook",
        raw=data,
        reference=reference,
        paid_at=str(data.get("occurred_at") or "").strip(),
    )