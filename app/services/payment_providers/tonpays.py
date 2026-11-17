"""TonPays PSP adapter — request → redirect → server-side verify (amounts in Toman).

Follows the TonPays merchant API:
  POST https://tonpays.online/api/v1/invoices/create  JSON body → invoice_id, invoice_url
  GET  https://tonpays.online/api/v1/invoices/check/{invoice_id}  read-only verify

Webhook: POST to callback_url with X-TonPays-Signature (t=<unix>,v1=<hmac_sha256_hex> on timestamp + "." + raw_body)

Verify never cancels or refunds; callers must match amount and invoice_id locally.
Unknown / transport failures must leave the settlement open for retry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse, quote

import httpx

from app.services.payment_providers.base import CheckoutRequest, CheckoutResult, VerifyResult

logger = logging.getLogger(__name__)

_MIN_AMOUNT_TOMAN = 1000
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_API_BASE = "https://tonpays.online"
_CREATE_URL = f"{_API_BASE}/api/v1/invoices/create"
_CHECK_URL = f"{_API_BASE}/api/v1/invoices/check"
_WEB_INVOICE_URL_TPL = "https://pay.tonpays.online/i/{invoice_id}"

# Webhook timestamp tolerance (seconds) — reject replayed / stale webhooks.
_WEBHOOK_TOLERANCE_SECONDS = 5 * 60

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
_OPEN_STATUSES = frozenset(
    {_STATUS_PENDING, _STATUS_PROCESSING, _STATUS_ACTION_LESS, _STATUS_ACTION_MORE}
)


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
    m = re.match(r"t=(\d+),v1=([0-9a-fA-F]+)", signature.strip())
    if not m:
        return False
    timestamp = int(m.group(1))
    expected_hmac = m.group(2).lower()
    # Timestamp tolerance: reject replayed / stale webhooks.
    now = int(time.time())
    if abs(now - timestamp) > _WEBHOOK_TOLERANCE_SECONDS:
        return False
    message = f"{timestamp}.".encode() + body
    computed = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest().lower()
    return hmac.compare_digest(computed, expected_hmac)


class TonPaysAdapter:
    name = "tonpays"

    def __init__(
        self,
        *,
        api_key: str,
        sandbox: bool = False,
        webhook_secret: str = "",
    ):
        self.api_key = (api_key or "").strip()
        self.sandbox = bool(sandbox)
        self.webhook_secret = (webhook_secret or "").strip()

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

        # Unique order_id per attempt so a retry is not refused as a duplicate.
        order_id = f"settlement-{settlement_id}-{uuid.uuid4().hex[:8]}"

        payload = {
            "amount": amount,
            "order_id": order_id,
            "callback_url": callback_url,
        }

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(_CREATE_URL, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("tonpays create network error: %s", exc)
            raise TonPaysError("خطا در ارتباط با درگاه تون پیز", admin_detail=str(exc))

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            detail = ""
            if isinstance(data, dict):
                detail_val = data.get("detail", "")
                if isinstance(detail_val, dict):
                    detail = str(
                        detail_val.get("message", "") or detail_val.get("detail", "") or ""
                    )
                elif isinstance(detail_val, str):
                    detail = detail_val
                else:
                    detail = str(data.get("detail", "") or "")
            logger.warning(
                "tonpays create failed status=%s detail=%s", resp.status_code, detail
            )
            raise TonPaysError(
                "درخواست تون پیز ناموفق بود",
                admin_detail=f"HTTP {resp.status_code}: {detail}",
            )

        if not isinstance(data, dict):
            raise TonPaysError(
                "پاسخ نامعتبر درگاه تون پیز", admin_detail="non-dict response"
            )

        invoice_id = str(data.get("invoice_id") or "").strip()
        if not invoice_id:
            raise TonPaysError(
                "درخواست تون پیز ناموفق بود", admin_detail="missing invoice_id"
            )

        checkout_url = str(
            data.get("web_invoice_url") or data.get("invoice_url") or ""
        ).strip()
        if not checkout_url:
            checkout_url = _WEB_INVOICE_URL_TPL.format(invoice_id=quote(invoice_id))

        # Validate checkout URL: https only, on expected host.
        try:
            parts = urlparse(checkout_url)
            if parts.scheme != "https" or not parts.netloc:
                raise ValueError("invalid checkout url")
            if "tonpays" not in parts.netloc:
                raise ValueError("unexpected checkout host")
        except ValueError:
            logger.warning("tonpays invalid checkout url: %s", checkout_url)
            checkout_url = _WEB_INVOICE_URL_TPL.format(invoice_id=quote(invoice_id))

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
            return VerifyResult(
                ok=False, external_ref="", message="شناسه فاکتور موجود نیست"
            )

        try:
            expected_amount = _safe_int(amount, field="مبلغ")
        except ValueError as exc:
            return VerifyResult(
                ok=False, external_ref=invoice_id, message=str(exc)
            )

        self._require_config()

        check_url = f"{_API_BASE}/api/v1/invoices/check/{quote(invoice_id)}"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(check_url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("tonpays verify network error: %s", exc)
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message="خطا در ارتباط با درگاه",
            )

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            logger.warning("tonpays verify error status=%s", resp.status_code)
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
            paid_amount_int = (
                _safe_int(paid_amount, field="مبلغ") if paid_amount is not None else None
            )
        except ValueError:
            paid_amount_int = None

        # expected_invoice_id is our settlement row id; TonPays does not echo it,
        # so we match by external_ref (the TonPays invoice_id) only.
        del expected_invoice_id

        if status in _COMPLETED_STATUSES and paid:
            # Never settle without an amount confirmed by the gateway.
            if paid_amount_int is None:
                return VerifyResult(
                    ok=False,
                    external_ref=invoice_id,
                    message="مبلغ تأییدشده از درگاه دریافت نشد",
                    review_reason="amount_missing",
                )
            if paid_amount_int != expected_amount:
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
                amount=paid_amount_int,
                message="verified",
                raw=data,
                reference=reference,
                paid_at="",
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
            message="وضعیت پرداخت قابل تشخیص نیست",
            raw=data,
        )

    async def verify_webhook(
        self,
        *,
        body: bytes,
        signature: str | None,
    ) -> VerifyResult | None:
        """Verify TonPays webhook payload.

        Signature is mandatory when webhook_secret is configured. The webhook only
        confirms authenticity — the amount is never trusted from the body; callers
        must re-verify server-side via verify().
        """
        if not self.webhook_secret:
            # No secret configured: webhook not secured, skip entirely.
            return None

        if not signature:
            return VerifyResult(
                ok=False,
                external_ref="",
                message="امضای وب‌هوک الزامی است",
            )

        if not _verify_signature(self.webhook_secret, body, signature):
            logger.warning("tonpays webhook signature verification failed")
            return VerifyResult(
                ok=False,
                external_ref="",
                message="امضای وب‌هوک نامعتبر",
            )

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return VerifyResult(
                ok=False,
                external_ref="",
                message="JSON نامعتبر",
            )

        if not isinstance(data, dict):
            return None

        event = str(data.get("event") or "").strip()
        invoice_id = str(data.get("invoice_id") or "").strip()
        paid = bool(data.get("paid"))
        status = str(data.get("status") or "").strip()

        if event != "invoice.completed" or not paid or status != _STATUS_COMPLETED:
            return None

        # Confirm authenticity only; amount comes from server-side verify().
        return VerifyResult(
            ok=True,
            external_ref=invoice_id,
            amount=None,
            message="webhook authentic",
            raw=data,
            reference=invoice_id,
            paid_at=str(data.get("occurred_at") or "").strip(),
        )
