"""BezanBeCard (بزن به کارت) HTTP client: create an order, then read its status.

The API (OpenAPI 0.1.0) authenticates with an ``x-api-key`` header; a key carries full
merchant authority, so it is never shown or logged. Amounts on the wire are Iranian Rial
(10,000 to 1,000,000,000); the bot stores Toman. Callbacks carry no signature, so payment
truth always comes from ``GET /api/v1/public/orders/{token}`` with the shop's own key.

Every call ends in one of three ways, and callers must keep them apart:
  - a result;
  - ``BbcRejected``: the API answered and refused (bad key, validation, unknown token);
    nothing was created, so the caller may give up;
  - ``BbcUnreachable``: no usable answer (network, timeout, 5xx, rate limit, non-JSON);
    the order may exist, so the caller must keep its saved request and retry it as is.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.bezanbecard.ir"
SITE_URL = "https://bezanbecard.ir"
PROVIDER_NAME = "bezanbecard"

SETUP_HELP_HTML = (
    f"سایت: <code>{SITE_URL}</code>\n"
    "<b>آموزش کوتاه</b>\n"
    "۱) در سایت ثبت‌نام کنید\n"
    "۲) کارت مقصد را در داشبورد ثبت کنید\n"
    "۳) طرح دارای API بگیرید (طرح رایگان API ندارد)\n"
    "۴) کلید API بسازید و اینجا بگذارید\n"
    "۵) روش «بزن به کارت» را روشن کنید\n\n"
    "تأیید و تحویل در ربات فقط با استعلام از <b>API</b> است. "
    "سفارش «نیاز به بررسی» را در داشبورد بزن به کارت تأیید کنید؛ ربات خودش تحویل می‌دهد."
)

STATUS_PAID = "paid"
STATUS_PENDING = "pending"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_EXPIRED = "expired"
STATUSES = frozenset({STATUS_PAID, STATUS_PENDING, STATUS_NEEDS_REVIEW, STATUS_EXPIRED})

MIN_RIAL = 10_000
MAX_RIAL = 1_000_000_000
MAX_MERCHANT_ORDER_ID = 64
MAX_DESCRIPTION = 500
MAX_URL = 2048
MAX_IDEMPOTENCY_KEY = 255
MAX_TOKEN = 128
MAX_REASON = 300

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# 408 and 429 mean "not processed yet, try again"; every other 4xx is a final refusal.
_RETRYABLE_4XX = frozenset({408, 429})

# The API's error ``code`` values, shown as fixed texts; its ``detail`` is only logged.
_ERROR_TEXTS = {
    "unauthenticated": "کلید API بزن به کارت نامعتبر است.",
    "forbidden": "حساب بزن به کارت اجازهٔ این کار را ندارد. طرح دارای API لازم است.",
    "validation_error": "درگاه بزن به کارت این پرداخت را نپذیرفت.",
    "not_found": "سفارش در بزن به کارت پیدا نشد.",
    "rate_limited": "درگاه بزن به کارت شلوغ است؛ چند لحظهٔ دیگر دوباره امتحان کنید.",
}
REJECTED_TEXT = "درگاه بزن به کارت این پرداخت را نپذیرفت."
UNREACHABLE_TEXT = "ارتباط با بزن به کارت برقرار نشد؛ چند لحظهٔ دیگر دوباره امتحان کنید."

# The callback and return links carry ``?t=<token>``; mask it if an error message quotes a link.
_LINK_TOKEN = re.compile(r"([?&]t=)[^&\s\"']+")

_client: httpx.AsyncClient | None = None


class BbcError(ValueError):
    """Base of the client's errors; ``str()`` is always a fixed, customer-safe text."""


class BbcRejected(BbcError):
    """The API answered with a final refusal; nothing was created or changed.

    ``reason`` is the API's own short explanation for the shop's staff (plain text,
    never shown to customers); ``str()`` stays the fixed customer text.
    """

    def __init__(self, message: str, *, status_code: int, code: str = "", reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.reason = reason


class BbcUnreachable(BbcError):
    """No usable answer: the request may or may not have taken effect."""


@dataclass(frozen=True, slots=True)
class BbcOrderRequest:
    """The exact body of one create call; a retry must send it again unchanged."""

    amount_rial: int
    merchant_order_id: str
    callback_url: str
    return_url: str
    description: str
    idempotency_key: str

    def body(self) -> dict[str, Any]:
        return {
            "amount": self.amount_rial,
            "merchant_order_id": self.merchant_order_id,
            "callback_url": self.callback_url,
            "return_url": self.return_url,
            "description": self.description,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "idempotency_key": self.idempotency_key}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BbcOrderRequest:
        return cls(
            amount_rial=int(data["amount"]),
            merchant_order_id=str(data["merchant_order_id"]),
            callback_url=str(data["callback_url"]),
            return_url=str(data["return_url"]),
            description=str(data["description"]),
            idempotency_key=str(data["idempotency_key"]),
        )


@dataclass(frozen=True, slots=True)
class BbcOrder:
    """An order as the API reports it, from create or from a status read."""

    order_id: str
    payment_token: str
    status: str
    unique_amount_rial: int | None
    base_amount_rial: int | None
    merchant_order_id: str | None
    payment_url: str = ""


def toman_to_rial(amount_toman: int) -> int:
    """Catalog amounts are Toman; BezanBeCard takes Rial within its limits."""
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int):
        raise ValueError("مبلغ نامعتبر")
    rial = amount_toman * 10
    if rial < MIN_RIAL:
        raise ValueError("مبلغ کمتر از حداقل درگاه بزن به کارت است")
    if rial > MAX_RIAL:
        raise ValueError("مبلغ بیشتر از سقف درگاه بزن به کارت است")
    return rial


def format_unique_rial(amount_rial: int) -> str:
    """The exact transfer amount in Rial; its last digits identify the payment."""
    from app.services.formatting import format_number

    return f"‏{format_number(int(amount_rial))} ریال"


def build_order_request(
    *,
    amount_toman: int,
    merchant_order_id: str,
    callback_url: str,
    return_url: str,
    description: str,
    idempotency_key: str,
) -> BbcOrderRequest:
    """Validate and clamp a create request to the API's documented limits."""
    if not callback_url.startswith("https://") or not return_url.startswith("https://"):
        raise ValueError("آدرس بازگشت درگاه باید https باشد")
    if len(callback_url) > MAX_URL or len(return_url) > MAX_URL:
        raise ValueError("آدرس بازگشت درگاه بیش از حد طولانی است")
    return BbcOrderRequest(
        amount_rial=toman_to_rial(amount_toman),
        merchant_order_id=str(merchant_order_id)[:MAX_MERCHANT_ORDER_ID],
        callback_url=callback_url,
        return_url=return_url,
        description=(description or "")[:MAX_DESCRIPTION],
        idempotency_key=idempotency_key[:MAX_IDEMPOTENCY_KEY],
    )


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=API_BASE, timeout=_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _headers(api_key: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    key = (api_key or "").strip()
    if len(key) < 8:
        raise BbcRejected("کلید API بزن به کارت تنظیم نشده", status_code=0, code="unauthenticated")
    headers = {"x-api-key": key, "Accept": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _call(method: str, path: str, *, headers: dict[str, str], json: dict | None = None) -> dict[str, Any]:
    """One request; a dict on 2xx, else ``BbcRejected`` or ``BbcUnreachable``."""
    try:
        resp = await _http().request(method, path, headers=headers, json=json)
    except httpx.HTTPError as exc:
        logger.warning("bezanbecard %s %s failed: %s", method, path.split("/")[-1][:8], type(exc).__name__)
        raise BbcUnreachable(UNREACHABLE_TEXT) from None
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = None
    if resp.status_code >= 500 or resp.status_code in _RETRYABLE_4XX:
        logger.warning("bezanbecard %s answered %s", method, resp.status_code)
        raise BbcUnreachable(UNREACHABLE_TEXT)
    if resp.status_code >= 400:
        code = _plain(data.get("code") or "", 40) if isinstance(data, dict) else ""
        reason = refusal_reason(data)
        logger.warning("bezanbecard %s refused %s code=%s reason=%s", method, resp.status_code, code, reason)
        raise BbcRejected(
            _ERROR_TEXTS.get(code, REJECTED_TEXT), status_code=resp.status_code, code=code, reason=reason,
        )
    if not isinstance(data, dict):
        logger.warning("bezanbecard %s answered %s without a JSON object", method, resp.status_code)
        raise BbcUnreachable(UNREACHABLE_TEXT)
    return data


def _plain(value: Any, limit: int) -> str:
    printable = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return _LINK_TOKEN.sub(r"\1***", " ".join(printable.split()))[:limit]


def _validation_item(item: Any) -> str:
    if not isinstance(item, dict):
        return _plain(item, 120)
    loc = item.get("loc")
    field = ".".join(str(part) for part in loc if part != "body") if isinstance(loc, list) else ""
    message = _plain(item.get("msg") or item.get("message") or "", 120)
    return f"{field}: {message}" if field else message


def refusal_reason(data: Any) -> str:
    """The API's explanation of a refusal in one short line: which field and why.

    Handles ``detail`` as text or as a list of ``{loc, msg}`` items, and a
    ``message``/``error`` field. Echoed request values (``input``) are left out and a
    link token quoted in a message is masked; the API key is a header and never echoed.
    """
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if isinstance(detail, list):
        text = "؛ ".join(filter(None, (_validation_item(item) for item in detail[:5])))
    elif isinstance(detail, dict):
        text = _validation_item(detail)
    else:
        text = detail or data.get("message") or data.get("error") or ""
    return _plain(text, MAX_REASON)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _order_from(data: dict[str, Any], *, token: str = "") -> BbcOrder:
    merchant = data.get("merchant_order_id")
    return BbcOrder(
        order_id=str(data.get("order_id") or "")[:MAX_TOKEN],
        payment_token=str(data.get("payment_token") or token).strip()[:MAX_TOKEN],
        status=str(data.get("status") or "").strip().lower(),
        unique_amount_rial=_int_or_none(data.get("unique_amount")),
        base_amount_rial=_int_or_none(data.get("base_amount")),
        merchant_order_id=str(merchant).strip()[:MAX_MERCHANT_ORDER_ID] if merchant is not None else None,
        payment_url=str(data.get("payment_url") or "").strip()[:MAX_URL],
    )


async def create_order(*, api_key: str, request: BbcOrderRequest) -> BbcOrder:
    """``POST /api/v1/public/orders``; replaying the same key and body returns the same order."""
    data = await _call(
        "POST",
        "/api/v1/public/orders",
        headers=_headers(api_key, idempotency_key=request.idempotency_key),
        json=request.body(),
    )
    order = _order_from(data)
    if (
        not order.payment_token
        or not order.order_id
        or not order.payment_url.startswith("https://")
        or not order.unique_amount_rial
        or order.unique_amount_rial <= 0
        or order.status not in STATUSES
    ):
        # A 2xx we cannot use: the order exists remotely, so this is not a refusal.
        logger.warning("bezanbecard create answered an incomplete order")
        raise BbcUnreachable(UNREACHABLE_TEXT)
    return order


async def get_order(*, api_key: str, payment_token: str) -> BbcOrder:
    """``GET /api/v1/public/orders/{token}``; 404 also covers another merchant's token."""
    token = (payment_token or "").strip()
    if len(token) < 8 or len(token) > MAX_TOKEN:
        raise BbcRejected("توکن پرداخت نامعتبر است", status_code=0, code="not_found")
    order = _order_from(await _call("GET", f"/api/v1/public/orders/{token}", headers=_headers(api_key)), token=token)
    if order.status not in STATUSES:
        logger.warning("bezanbecard order has an unknown status")
        raise BbcUnreachable(UNREACHABLE_TEXT)
    return order

