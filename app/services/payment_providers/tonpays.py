"""TonPays PSP adapter — request → redirect → server-side verify (amounts in Toman).

Follows the TonPays merchant API:
  POST https://tonpays.online/api/v1/invoices/create  JSON body → invoice_id, invoice_url
  GET  https://tonpays.online/api/v1/invoices/check/{invoice_id}  read-only verify

Webhook: POST to callback_url with X-TonPays-Signature
  t=<unix>,v1=<hmac_sha256_hex> on timestamp + "." + raw_body

Security properties:
  * Webhook signature is mandatory when webhook_secret is configured.
  * Webhook timestamp is checked against a 5-minute tolerance (replay protection).
  * The amount is NEVER trusted from the webhook body; callers must re-verify
    server-side via verify() before settling.
  * Checkout URLs are validated: https only, host must be tonpays.online exactly.
  * All error messages shown to customers are generic; provider text is never echoed.
  * Network failures leave the settlement open for retry (never settle on error).
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

# ── Constants ──────────────────────────────────────────────────────────────

_MIN_AMOUNT_TOMAN = 1_000
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_API_BASE = "https://tonpays.online"
_API_HOST = "tonpays.online"
_CREATE_URL = f"{_API_BASE}/api/v1/invoices/create"
_CHECK_URL_TEMPLATE = f"{_API_BASE}/api/v1/invoices/check/{{invoice_id}}"
_WEB_INVOICE_URL_TPL = "https://pay.tonpays.online/i/{invoice_id}"

# Webhook timestamp tolerance (5 minutes) — reject replayed / stale webhooks.
_WEBHOOK_TOLERANCE_SECONDS = 5 * 60

# Invoice statuses from TonPays merchant documentation.
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


# Customer-safe generic messages (never echo provider text).
_MSG_INVALID_SIGNATURE = "امضای وب‌هوک نامعتبر است"
_MSG_MISSING_SIGNATURE = "امضای وب‌هوک الزامی است"
_MSG_NETWORK_ERROR = "خطا در ارتباط با درگاه"
_MSG_INVALID_JSON = "JSON نامعتبر"
_MSG_AMOUNT_MISSING = "مبلغ تأییدشده از درگاه دریافت نشد"
_MSG_AMOUNT_MISMATCH = "مبلغ تأییدشده با سفارش هم‌خوانی ندارد"
_MSG_NOT_PAID = "پرداختی برای این تراکنش در درگاه ثبت نشده است"
_MSG_FAILED = "پرداخت تکمیل نشد یا رد شده"
_MSG_UNKNOWN_STATUS = "وضعیت پرداخت قابل تشخیص نیست"
_MSG_NO_INVOICE = "شناسه فاکتور موجود نیست"
_MSG_API_KEY_MISSING = "کلید API تون پیز تنظیم نشده"
_MSG_MIN_AMOUNT = "حداقل مبلغ تون پیز {minimum} تومان است"
_MSG_INVALID_FIELD_FA = "{field} نامconsider است"
_MSG_CHECKOUT_FAILED = "درخواست تون پیز نامconsider بود"
_MSG_INVALID_CHECKOUT_URL = "آدرس پرداخت نامconsider است"


def _safe_int(value: Any, *, field: str) -> int:
    """Parse an integer strictly; booleans and non-integral floats are rejected."""
    if isinstance(value, bool) or value is None:
        raise ValueError(_MSG_INVALID_FIELD_FA.format(field=field))
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(_MSG_INVALID_FIELD_FA.format(field=field))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(_MSG_INVALID_FIELD_FA.format(field=field)) from exc


def _validate_callback_url(value: str) -> str:
    """Validate that the callback URL uses https and points to a public host."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("آدرس وب‌هوک تنظیم نشده")
    try:
        parts = urlparse(raw)
    except ValueError as exc:
        raise ValueError("آدرس وب‌هوک ناماقعرست است") from exc
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("آدرس وب‌هوک باید با https و میزبان عمومی باشد")
    return raw


def _is_trusted_host(netloc: str, trusted: str) -> bool:
    """Exact host match or subdomain of the trusted host."""
    return netloc == trusted or netloc.endswith("." + trusted)


def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Verify X-TonPays-Signature: t=<unix>,v1=<hmac_sha256_hex> on timestamp + '.' + raw_body.

    Rejects:
      * missing secret or signature
      * malformed signature format
      * timestamps outside the tolerance window (replay protection)
      * incorrect HMAC
    """
    if not secret or not signature:
        return False

    m = re.match(r"t=(\d+),v1=([0-9a-fA-F]+)", signature.strip())
    if not m:
        return False

    timestamp = int(m.group(1))
    expected_hmac = m.group(2).lower()

    # Replay protection: only accept timestamps within the tolerance window.
    # Small positive skew (future timestamps) is tolerated for clock drift.
    now = int(time.time())
    if now - timestamp > _WEBHOOK_TOLERANCE_SECONDS:
        return False
    if timestamp - now > _WEBHOOK_TOLERANCE_SECONDS:
        return False

    message = f"{timestamp}.".encode() + body
    computed = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest().lower()
    return hmac.compare_digest(computed, expected_hmac)


def _safe_detail(data: Any) -> str:
    """Extract a safe detail string from a provider error response.

    Never returns raw provider text that might leak internal implementation
    details to the customer.
    """
    if isinstance(data, dict):
        detail = data.get("detail", "")
        if isinstance(detail, dict):
            return str(detail.get("message", "") or detail.get("detail", "") or "")
        if isinstance(detail, str):
            return detail[:200]
        return str(detail)[:200]
    return ""


class TonPaysAdapter:
    """TonPays payment gateway adapter.

    Implements the PspAdapter protocol plus an optional webhook verification
    entry point. Amounts are always in Toman (integer).
    """

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

    # ── Protocol: PspAdapter ──────────────────────────────────────────────

    def _require_config(self) -> None:
        if not self.api_key:
            raise ValueError(_MSG_API_KEY_MISSING)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        """Create a TonPays invoice and return a checkout URL.

        Raises TonPaysError on configuration, validation, or provider errors.
        Network failures raise TonPaysError with a generic customer message
        and the underlying exception in admin_detail.
        """
        self._require_config()

        amount = _safe_int(req.amount, field="مبلغ")
        if amount < _MIN_AMOUNT_TOMAN:
            raise ValueError(_MSG_MIN_AMOUNT.format(minimum=_MIN_AMOUNT_TOMAN))

        settlement_id = _safe_int(req.settlement_id, field="شناسه تسویه")
        if settlement_id <= 0:
            raise ValueError("شناسه تسویه باید بزرگ‌تر از صفر باشد")

        callback_url = _validate_callback_url(req.callback_url)

        # Unique order_id per attempt so a retry is not refused as a duplicate.
        # Use a full UUID hex for sufficient entropy (not 8 chars).
        order_id = f"settlement-{settlement_id}-{uuid.uuid4().hex}"

        payload = {
            "amount": amount,
            "order_id": order_id,
            "callback_url": callback_url,
        }

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(_CREATE_URL, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("tonpays create network error: %s", type(exc).__name__)
            raise TonPaysError(_MSG_NETWORK_ERROR, admin_detail=str(exc))

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            detail = _safe_detail(data)
            logger.warning(
                "tonpays create failed status=%s", resp.status_code,
            )
            raise TonPaysError(
                _MSG_CHECKOUT_FAILED,
                admin_detail=f"HTTP {resp.status_code}: {detail}",
            )

        if not isinstance(data, dict):
            raise TonPaysError(
                "پاسخ نامعتبر درگاه تون پیز", admin_detail="non-dict response"
            )

        invoice_id = str(data.get("invoice_id") or "").strip()
        if not invoice_id:
            raise TonPaysError(
                _MSG_CHECKOUT_FAILED, admin_detail="missing invoice_id"
            )

        checkout_url = str(
            data.get("web_invoice_url") or data.get("invoice_url") or ""
        ).strip()
        if not checkout_url:
            checkout_url = _WEB_INVOICE_URL_TPL.format(invoice_id=quote(invoice_id))

        # Validate checkout URL: https only, host must be tonpays.online exactly.
        try:
            parts = urlparse(checkout_url)
            if parts.scheme != "https" or not parts.netloc:
                raise ValueError(_MSG_INVALID_CHECKOUT_URL)
            if not _is_trusted_host(parts.netloc, _API_HOST):
                raise ValueError(_MSG_INVALID_CHECKOUT_URL)
        except ValueError:
            logger.warning("tonpays invalid checkout url: %s", checkout_url)
            checkout_url = _WEB_INVOICE_URL_TPL.format(invoice_id=quote(invoice_id))

        return CheckoutResult(
            external_ref=invoice_id,
            checkout_url=checkout_url,
            raw={},  # Never store raw provider response (may contain secrets).
        )

    async def verify(
        self,
        *,
        external_ref: str,
        amount: int,
        callback_params: dict[str, Any],
        expected_invoice_id: int | None = None,
    ) -> VerifyResult:
        """Server-side verification of a payment via TonPays check API.

        Never settles without a gateway-confirmed amount. Network failures
        return ok=False (unknown outcome) so the settlement stays open for
        retry — we never settle on a transport error.
        """
        del callback_params  # TonPays does not echo callback params; verified via API.
        del expected_invoice_id  # TonPays does not echo our settlement row id.

        invoice_id = str(external_ref or "").strip()
        if not invoice_id:
            return VerifyResult(
                ok=False,
                external_ref="",
                message=_MSG_NO_INVOICE,
            )

        try:
            expected_amount = _safe_int(amount, field="مبلغ")
        except ValueError as exc:
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message=str(exc),
            )

        self._require_config()

        check_url = _CHECK_URL_TEMPLATE.format(invoice_id=quote(invoice_id))
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(check_url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("tonpays verify network error: %s", type(exc).__name__)
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message=_MSG_NETWORK_ERROR,
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
                message=_MSG_NETWORK_ERROR,
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
                _safe_int(paid_amount, field="مبلغ")
                if paid_amount is not None
                else None
            )
        except ValueError:
            paid_amount_int = None

        if status in _COMPLETED_STATUSES and paid:
            # Never settle without an amount confirmed by the gateway.
            if paid_amount_int is None:
                return VerifyResult(
                    ok=False,
                    external_ref=invoice_id,
                    message=_MSG_AMOUNT_MISSING,
                    review_reason="amount_missing",
                )
            if paid_amount_int != expected_amount:
                return VerifyResult(
                    ok=False,
                    external_ref=invoice_id,
                    amount=paid_amount_int,
                    message=_MSG_AMOUNT_MISMATCH,
                    reference=str(data.get("invoice_id") or "").strip(),
                    review_reason="amount_mismatch",
                )
            reference = str(data.get("invoice_id") or "").strip()
            return VerifyResult(
                ok=True,
                external_ref=invoice_id,
                amount=paid_amount_int,
                message="verified",
                reference=reference,
                paid_at="",
            )

        if status in _FAILED_STATUSES:
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message=_MSG_FAILED,
            )

        if status in _OPEN_STATUSES:
            return VerifyResult(
                ok=False,
                external_ref=invoice_id,
                message=_MSG_NOT_PAID,
            )

        return VerifyResult(
            ok=False,
            external_ref=invoice_id,
            message=_MSG_UNKNOWN_STATUS,
        )

    # ── Webhook verification (TonPays-specific extension) ─────────────────

    async def verify_webhook(
        self,
        *,
        body: bytes,
        signature: str | None,
    ) -> VerifyResult | None:
        """Verify a TonPays webhook payload.

        Returns:
            None — no webhook_secret configured; webhook not secured, skip.
            VerifyResult(ok=False, ...) — signature missing or invalid.
            VerifyResult(ok=True, amount=None, ...) — webhook authentic.
                The amount is NOT trusted from the body; callers must
                re-verify server-side via verify() before settling.
        """
        if not self.webhook_secret:
            return None

        if not signature:
            return VerifyResult(
                ok=False,
                external_ref="",
                message=_MSG_MISSING_SIGNATURE,
            )

        if not _verify_signature(self.webhook_secret, body, signature):
            logger.warning("tonpays webhook signature verification failed")
            return VerifyResult(
                ok=False,
                external_ref="",
                message=_MSG_INVALID_SIGNATURE,
            )

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return VerifyResult(
                ok=False,
                external_ref="",
                message=_MSG_INVALID_JSON,
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
            reference=invoice_id,
            paid_at=str(data.get("occurred_at") or "").strip(),
        )


async def verify_tonpays_webhook(
    *,
    api_key: str,
    webhook_secret: str,
    body: bytes,
    signature: str | None,
) -> VerifyResult | None:
    """Module-level webhook verification for TonPays.

    Avoids requiring callers to import TonPaysAdapter or use isinstance checks.
    """
    adapter = TonPaysAdapter(
        api_key=api_key,
        webhook_secret=webhook_secret,
    )
    return await adapter.verify_webhook(body=body, signature=signature)
