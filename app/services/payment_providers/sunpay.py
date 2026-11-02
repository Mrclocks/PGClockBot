"""SunPay PSP adapter — request → redirect → callback → verify (amounts in Toman).

Follows the SunPay merchant API:
  GET|POST {PAY_URL}/api/request   JSON body → token
  GET  {PAY_URL}/pay/{token}   browser checkout
  GET/POST {PAY_URL}/api/verified  JSON body → amount/reference (read-only)

Verify never cancels or refunds; callers must match amount and invoice_id locally.
Unknown / transport failures must leave the settlement open for retry.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.payment_providers.base import CheckoutRequest, CheckoutResult, VerifyResult
from app.services.redact import redact

logger = logging.getLogger(__name__)

MIN_AMOUNT_TOMAN = 1000
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_PLACEHOLDER_KEYS = frozenset(
    {"", "changeme", "your-api-key", "YOUR_API_KEY", "your_api_key"}
)
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
    }
)

# Documented default is GET+JSON; POST is available after live confirmation.
REQUEST_METHOD_GET = "GET"
REQUEST_METHOD_POST = "POST"
DEFAULT_REQUEST_METHOD = REQUEST_METHOD_GET
_ALLOWED_REQUEST_METHODS = frozenset({REQUEST_METHOD_GET, REQUEST_METHOD_POST})

CUSTOMER_REQUEST_FAILED = "درخواست سان‌پی ناموفق بود"
CUSTOMER_UNAVAILABLE = "این روش پرداخت موقتاً در دسترس نیست؛ روش دیگری را انتخاب کنید."
CUSTOMER_NETWORK = "خطا در ارتباط با درگاه سان‌پی"
CUSTOMER_BAD_RESPONSE = "پاسخ نامعتبر درگاه سان‌پی"
CUSTOMER_NOT_PAID = "پرداختی برای این تراکنش در درگاه ثبت نشده است"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BODY_LOG_LIMIT = 300
_MSG_LOG_LIMIT = 120

_client: httpx.AsyncClient | None = None


def normalize_request_method(raw: str | None) -> str:
    method = (raw or "").strip().upper()
    return method if method in _ALLOWED_REQUEST_METHODS else DEFAULT_REQUEST_METHOD


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _is_success_flag(raw: Any) -> bool:
    """SunPay documents success as 1 only; accept the int, its string form, or JSON true."""
    if raw is True:
        return True
    if isinstance(raw, bool) or raw is None:
        return False
    if isinstance(raw, int):
        return raw == 1
    return str(raw).strip() == "1"


def _missing_paid_evidence(result: dict[str, Any]) -> bool:
    """Verify is a lookup, not a confirmation: a paid transaction carries a reference.

    ``paid_at`` is checked only when the gateway sends the key, so a response that
    omits it does not block a payment that has a reference.
    """
    if not str(result.get("reference") or "").strip():
        return True
    return "paid_at" in result and not str(result.get("paid_at") or "").strip()


def _host_is_blocked(host: str) -> bool:
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return True
    if name in _BLOCKED_HOSTS or name.endswith(".localhost"):
        return True
    # Strip brackets from IPv6 literals.
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return _ip_is_blocked(ip)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Non-global covers private, loopback, link-local, multicast, and reserved docs ranges.
    return not bool(ip.is_global)


def validate_sunpay_pay_url(value: str) -> str:
    """Normalize PAY_URL: https only, no localhost/private literals (SSRF / cleartext key)."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("آدرس پایه سان‌پی (PAY_URL) تنظیم نشده")
    try:
        parts = urlparse(raw)
    except ValueError as exc:
        raise ValueError("آدرس پایه سان‌پی نامعتبر است") from exc
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("آدرس پایه سان‌پی باید با https و میزبان عمومی باشد")
    if parts.username or parts.password:
        raise ValueError("آدرس پایه سان‌پی نامعتبر است")
    host = (parts.hostname or "").strip().lower()
    if _host_is_blocked(host):
        raise ValueError("آدرس پایه سان‌پی به میزبان داخلی مجاز نیست")
    return raw


def _resolve_host_addresses(host: str) -> list[str]:
    """Blocking DNS lookup — call via asyncio.to_thread from async paths."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addrs: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.add(addr)
            addrs.append(addr)
    return addrs


async def ensure_sunpay_pay_host_public(host: str) -> None:
    """Reject hosts whose current DNS answers include a non-public address."""
    name = (host or "").strip().lower().rstrip(".")
    if not name or _host_is_blocked(name):
        raise ValueError("آدرس پایه سان‌پی به میزبان داخلی مجاز نیست")
    try:
        addrs = await asyncio.to_thread(_resolve_host_addresses, name)
    except OSError as exc:
        raise ValueError("آدرس پایه سان‌پی قابل resolve نیست") from exc
    if not addrs:
        raise ValueError("آدرس پایه سان‌پی قابل resolve نیست")
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            raise ValueError("آدرس پایه سان‌پی به میزبان داخلی مجاز نیست")


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


def _scrub_known_secrets(text: str, *secrets: str) -> str:
    """Remove our own secret values even when the gateway omits the field name."""
    out = text
    for secret in secrets:
        value = (secret or "").strip()
        if len(value) < 4:
            continue
        out = out.replace(value, "<redacted>")
    return out


def _plain_snippet(
    text: object,
    *,
    limit: int = _MSG_LOG_LIMIT,
    secrets: tuple[str, ...] = (),
) -> str:
    raw = html.unescape("" if text is None else str(text))
    raw = _HTML_TAG_RE.sub("", raw)
    raw = _scrub_known_secrets(raw, *secrets)
    return redact(raw, limit=limit)


def _response_body_snippet(
    resp: httpx.Response,
    *,
    secrets: tuple[str, ...] = (),
) -> str:
    try:
        text = resp.text
    except Exception:
        text = ""
    return _plain_snippet(text or "", limit=_BODY_LOG_LIMIT, secrets=secrets)


def _log_failed_response(
    phase: str,
    resp: httpx.Response,
    *,
    note: str = "",
    secrets: tuple[str, ...] = (),
) -> None:
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    snippet = _response_body_snippet(resp, secrets=secrets)
    safe_note = _plain_snippet(note, limit=_MSG_LOG_LIMIT, secrets=secrets) if note else ""
    extra = f" note={safe_note}" if safe_note else ""
    logger.warning(
        "sunpay %s failed status=%s content_type=%s body=%s%s",
        phase,
        resp.status_code,
        ctype or "-",
        snippet or "-",
        extra,
    )


def _gateway_error_note(data: Any, *, secrets: tuple[str, ...] = ()) -> str:
    """Safe detail for logs / admin error_message; never for customers alone."""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        code = str(err.get("code") or "").strip()
        msg = _plain_snippet(err.get("message") or "", limit=_MSG_LOG_LIMIT, secrets=secrets)
        parts = [p for p in (code, msg) if p]
        return " ".join(parts)
    if isinstance(err, str) and err.strip():
        return _plain_snippet(err, limit=_MSG_LOG_LIMIT, secrets=secrets)
    top = data.get("message")
    if isinstance(top, str) and top.strip():
        return _plain_snippet(top, limit=_MSG_LOG_LIMIT, secrets=secrets)
    return ""


def _api_error_text(data: Any) -> str:
    """Neutral customer-facing text; codes stay in logs / admin_detail."""
    if not isinstance(data, dict):
        return CUSTOMER_BAD_RESPONSE
    return CUSTOMER_REQUEST_FAILED


class SunPayGatewayError(ValueError):
    """Customer-safe message with optional admin/log detail."""

    def __init__(self, customer: str, *, admin_detail: str = "") -> None:
        super().__init__(customer)
        self.admin_detail = (admin_detail or customer).strip()


def _parse_verified_payment(
    data: dict[str, Any],
    *,
    token: str,
    expected_amount: int,
    secrets: tuple[str, ...] = (),
) -> VerifyResult | tuple[dict[str, Any], int]:
    """Return paid (result, amount) or a failed VerifyResult."""
    if not _is_success_flag(data.get("success")):
        note = _gateway_error_note(data, secrets=secrets)
        if note:
            logger.warning("sunpay verify gateway error detail=%s", note)
        return VerifyResult(
            ok=False,
            external_ref=token,
            message=_api_error_text(data),
            raw=data,
        )
    result = data.get("data") if isinstance(data.get("data"), dict) else {}
    if _missing_paid_evidence(result):
        logger.warning("sunpay verify success without paid reference token=%s", token[:16])
        return VerifyResult(
            ok=False,
            external_ref=token,
            message=CUSTOMER_NOT_PAID,
            raw=data,
        )
    try:
        paid_amount = _safe_int(result.get("amount"), field="مبلغ")
    except ValueError:
        return VerifyResult(
            ok=False,
            external_ref=token,
            message="مبلغ تأییدشده نامعتبر است",
            raw=data,
        )
    if paid_amount != expected_amount:
        logger.warning(
            "sunpay verify amount mismatch token=%s expected=%s paid=%s",
            token[:16],
            expected_amount,
            paid_amount,
        )
        return VerifyResult(
            ok=False,
            external_ref=token,
            amount=paid_amount,
            message="مبلغ تأییدشده با سفارش هم‌خوانی ندارد",
            raw=data,
            reference=str(result.get("reference") or "").strip(),
            review_reason="amount_mismatch",
        )
    return result, paid_amount


def _invoice_matches_settlement(
    result: dict[str, Any],
    *,
    token: str,
    paid_amount: int,
    data: dict[str, Any],
    expected_invoice_id: int | None,
) -> VerifyResult | None:
    """None when ok; otherwise a failed VerifyResult."""
    if expected_invoice_id is None:
        return None
    raw_invoice = result.get("invoice_id")
    if raw_invoice is None or str(raw_invoice).strip() == "":
        return VerifyResult(
            ok=False,
            external_ref=token,
            amount=paid_amount,
            message="شناسه فاکتور در پاسخ درگاه نیست",
            raw=data,
        )
    try:
        if _safe_int(raw_invoice, field="invoice_id") != int(expected_invoice_id):
            logger.warning(
                "sunpay verify invoice mismatch token=%s expected=%s got=%s",
                token[:16],
                expected_invoice_id,
                raw_invoice,
            )
            return VerifyResult(
                ok=False,
                external_ref=token,
                amount=paid_amount,
                message="شناسه فاکتور با تسویه هم‌خوانی ندارد",
                raw=data,
                reference=str(result.get("reference") or "").strip(),
                review_reason="invoice_mismatch",
            )
    except ValueError as exc:
        return VerifyResult(
            ok=False,
            external_ref=token,
            amount=paid_amount,
            message=str(exc),
            raw=data,
        )
    return None


class SunPayAdapter:
    name = "sunpay"

    def __init__(self, *, api_key: str, pay_url: str, request_method: str = DEFAULT_REQUEST_METHOD):
        self.api_key = (api_key or "").strip()
        # Store raw; validate on use so create_checkout can mark FAILED inside its try.
        self.pay_url = (pay_url or "").strip().rstrip("/")
        self.request_method = normalize_request_method(request_method)

    def _log_secrets(self, *extra: str) -> tuple[str, ...]:
        return (self.api_key, *(s for s in extra if s))

    def _require_config(self) -> None:
        if self.api_key in _PLACEHOLDER_KEYS:
            raise ValueError("کلید API سان‌پی تنظیم نشده")
        self.pay_url = validate_sunpay_pay_url(self.pay_url)

    async def _require_outbound_ready(self) -> None:
        self._require_config()
        host = (urlparse(self.pay_url).hostname or "").strip().lower()
        await ensure_sunpay_pay_host_public(host)

    def _request_url(self) -> str:
        return f"{self.pay_url}/api/request"

    def _verify_url(self) -> str:
        return f"{self.pay_url}/api/verified"

    def checkout_page_url(self, token: str) -> str:
        return f"{self.pay_url}/pay/{token}"

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        try:
            await self._require_outbound_ready()
        except ValueError as exc:
            # Missing key / bad PAY_URL is the shop's setup, not something the payer can fix.
            logger.warning("sunpay create refused: %s", exc)
            raise SunPayGatewayError(CUSTOMER_UNAVAILABLE, admin_detail=str(exc)) from exc
        amount = _safe_int(req.amount, field="مبلغ")
        if amount < MIN_AMOUNT_TOMAN:
            raise ValueError(f"حداقل مبلغ سان‌پی {MIN_AMOUNT_TOMAN} تومان است")
        invoice_id = _safe_int(req.settlement_id, field="invoice_id")
        if invoice_id <= 0:
            raise ValueError("invoice_id باید بزرگ‌تر از صفر باشد")
        callback = (req.callback_url or "").strip()
        # SunPay production callbacks must be https (panel TLS / PUBLIC_BASE_URL https).
        try:
            cb_parts = urlparse(callback)
        except ValueError as exc:
            raise ValueError("آدرس بازگشت سان‌پی نامعتبر است") from exc
        if cb_parts.scheme != "https" or not cb_parts.netloc:
            logger.warning(
                "sunpay create refused: callback is not https scheme=%s",
                cb_parts.scheme or "-",
            )
            raise SunPayGatewayError(
                CUSTOMER_UNAVAILABLE,
                admin_detail="callback must be https",
            )

        payload = {
            "api_key": self.api_key,
            "callback": callback,
            "amount": amount,
            "invoice_id": invoice_id,
        }
        secrets = self._log_secrets()
        try:
            resp = await _http().request(
                self.request_method,
                self._request_url(),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "sunpay request network error: %s",
                _plain_snippet(str(exc), limit=200, secrets=secrets),
            )
            raise SunPayGatewayError(CUSTOMER_NETWORK, admin_detail="network") from exc

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            _log_failed_response("request", resp, note="non-json", secrets=secrets)
            raise SunPayGatewayError(
                CUSTOMER_BAD_RESPONSE,
                admin_detail=f"HTTP {resp.status_code} non-json",
            ) from None

        if resp.status_code >= 400:
            note = _gateway_error_note(data, secrets=secrets) if isinstance(data, dict) else ""
            _log_failed_response(
                "request", resp, note=note or f"http_{resp.status_code}", secrets=secrets
            )
            raise SunPayGatewayError(
                CUSTOMER_REQUEST_FAILED,
                admin_detail=note or f"HTTP {resp.status_code}",
            )

        if not isinstance(data, dict) or not _is_success_flag(data.get("success")):
            note = _gateway_error_note(data, secrets=secrets) if isinstance(data, dict) else ""
            _log_failed_response(
                "request", resp, note=note or "success!=1", secrets=secrets
            )
            raise SunPayGatewayError(
                _api_error_text(data),
                admin_detail=note or "success!=1",
            )

        result = data.get("data") if isinstance(data.get("data"), dict) else {}
        token = str((result or {}).get("token") or "").strip()
        if not token:
            _log_failed_response("request", resp, note="missing token", secrets=secrets)
            raise SunPayGatewayError(
                CUSTOMER_REQUEST_FAILED,
                admin_detail="missing token",
            )
        return CheckoutResult(
            external_ref=token,
            checkout_url=self.checkout_page_url(token),
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
        token = str(external_ref or "").strip()
        cb_token = str(callback_params.get("token") or "").strip()
        if cb_token and token and cb_token.lower() != token.lower():
            return VerifyResult(
                ok=False,
                external_ref=token,
                message="توکن تراکنش با تسویه هم‌خوانی ندارد",
            )
        if cb_token and not token:
            token = cb_token
        if not token:
            return VerifyResult(ok=False, external_ref="", message="توکن تراکنش موجود نیست")

        # Docs §5.1: only look up / verify when callback success=1.
        if "success" not in callback_params or not _is_success_flag(
            callback_params.get("success")
        ):
            return VerifyResult(
                ok=False,
                external_ref=token,
                message="پرداخت توسط کاربر تکمیل نشد",
            )

        try:
            expected_amount = _safe_int(amount, field="مبلغ")
        except ValueError as exc:
            return VerifyResult(ok=False, external_ref=token, message=str(exc))

        try:
            await self._require_outbound_ready()
        except ValueError as exc:
            return VerifyResult(ok=False, external_ref=token, message=str(exc))

        secrets = self._log_secrets(token)
        payload = {"api_key": self.api_key, "token": token}
        try:
            # Docs allow GET or POST; POST keeps the JSON body reliable.
            resp = await _http().request(
                "POST",
                self._verify_url(),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "sunpay verify network error: %s",
                _plain_snippet(str(exc), limit=200, secrets=secrets),
            )
            return VerifyResult(
                ok=False,
                external_ref=token,
                message=CUSTOMER_NETWORK,
            )

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            _log_failed_response("verify", resp, note="non-json", secrets=secrets)
            return VerifyResult(
                ok=False,
                external_ref=token,
                message=CUSTOMER_BAD_RESPONSE,
            )

        if resp.status_code >= 400:
            note = _gateway_error_note(data, secrets=secrets) if isinstance(data, dict) else ""
            _log_failed_response(
                "verify", resp, note=note or f"http_{resp.status_code}", secrets=secrets
            )
            return VerifyResult(
                ok=False,
                external_ref=token,
                message=CUSTOMER_REQUEST_FAILED,
                raw=data if isinstance(data, dict) else None,
            )

        if not isinstance(data, dict):
            _log_failed_response("verify", resp, note="not-a-dict", secrets=secrets)
            return VerifyResult(
                ok=False,
                external_ref=token,
                message=CUSTOMER_BAD_RESPONSE,
            )

        parsed = _parse_verified_payment(
            data, token=token, expected_amount=expected_amount, secrets=secrets
        )
        if isinstance(parsed, VerifyResult):
            if not parsed.ok and parsed.raw is not None:
                note = _gateway_error_note(parsed.raw, secrets=secrets)
                if note:
                    _log_failed_response("verify", resp, note=note, secrets=secrets)
            return parsed
        result, paid_amount = parsed

        invoice_err = _invoice_matches_settlement(
            result,
            token=token,
            paid_amount=paid_amount,
            data=data,
            expected_invoice_id=expected_invoice_id,
        )
        if invoice_err is not None:
            return invoice_err

        reference = str(result.get("reference") or "").strip()
        return VerifyResult(
            ok=True,
            external_ref=token,
            amount=paid_amount,
            message=reference or "verified",
            raw=data,
            reference=reference,
            paid_at=str(result.get("paid_at") or "").strip(),
        )
