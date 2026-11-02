"""Turn a toman price into a crypto amount at a rate the shop chose, and lock it on the payment.

Each shop (``None`` = the platform) sets its own mode: off (the customer sees the toman price, as
before), a manual rate, or a rate read from its own HTTPS JSON API. A background job fetches API
rates, so a customer's tap never waits on a network call; an API rate that is stale, absurd or a
sudden jump is not used and the manual rate, if any, takes over. Amounts are integers in millionths
of the asset; no float is used for money. The quote (amount, rate, expiry) is saved on the payment
so staff review it against what the customer was shown, whatever the rate does afterwards.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, PaymentStatus
from app.services.url_safety import UnsafeUrlError, ensure_host_resolves_public, validate_public_https_url

logger = logging.getLogger(__name__)

MODE_KEY = "crypto_rate_mode"
ASSET_KEY = "crypto_rate_asset"
MANUAL_KEY = "crypto_rate_manual"
URL_KEY = "crypto_rate_api_url"
PATH_KEY = "crypto_rate_api_path"
DIVISOR_KEY = "crypto_rate_api_divisor"
MARGIN_KEY = "crypto_rate_margin_percent"
MINUTES_KEY = "crypto_quote_minutes"
MODE_OFF, MODE_MANUAL, MODE_API = "off", "manual", "api"

AMOUNT_SCALE = 10**6
RATE_SCALE = 10**4
DISPLAY_STEP = AMOUNT_SCALE // 100  # amounts are shown, and asked for, to two decimals
MAX_RATE = Decimal(10_000_000)
MAX_MARGIN_BPS = 2000
DEFAULT_MINUTES, MIN_MINUTES, MAX_MINUTES = 15, 1, 120
FETCH_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 64 * 1024
RATE_MAX_AGE_SECONDS = 30 * 60
JUMP_GUARD_SECONDS = 60 * 60
MAX_JUMP_RATIO = Decimal("0.25")
MAX_PATH_PARTS = 8
_NUMBER = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,6})?$")
_ASSET = re.compile(r"^[A-Za-z0-9]{2,16}$")


class RateUnavailable(ValueError):
    """No usable rate right now; the message is safe to show to the customer."""


UNAVAILABLE_TEXT = "نرخ ارز فعلاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید یا روش پرداخت دیگری را انتخاب کنید."


@dataclass(frozen=True, slots=True)
class RateConfig:
    mode: str
    asset: str
    manual_rate: Decimal | None
    api_url: str
    api_path: str
    divisor: Decimal
    margin_bps: int
    quote_minutes: int


@dataclass(frozen=True, slots=True)
class CryptoQuote:
    asset: str
    amount_micro: int
    rate_scaled: int
    expires_at: datetime


def parse_decimal(raw: object, *, maximum: Decimal = MAX_RATE) -> Decimal | None:
    """A positive plain decimal number from text (digits and one dot), else None."""
    text = str(raw or "").strip()
    if not _NUMBER.match(text):
        return None
    value = Decimal(text)
    return value if 0 < value <= maximum else None


def _bounded_int(raw: object, *, default: int, low: int, high: int) -> int:
    text = str(raw or "").strip()
    return min(max(int(text), low), high) if text.isascii() and text.isdigit() and len(text) <= 6 else default


def config_from_settings(settings: Mapping[str, str]) -> RateConfig | None:
    """The shop's conversion setup, or None while conversion is off or its asset is malformed."""
    mode = str(settings.get(MODE_KEY) or MODE_OFF).strip().lower()
    asset = str(settings.get(ASSET_KEY) or "USDT").strip().upper()
    if mode not in {MODE_MANUAL, MODE_API} or not _ASSET.match(asset):
        return None
    margin = parse_decimal(settings.get(MARGIN_KEY) or "0", maximum=Decimal(20))
    divisor = parse_decimal(settings.get(DIVISOR_KEY) or "1", maximum=Decimal(1_000_000)) or Decimal(1)
    return RateConfig(
        mode=mode,
        asset=asset,
        manual_rate=parse_decimal(settings.get(MANUAL_KEY)),
        api_url=str(settings.get(URL_KEY) or "").strip(),
        api_path=str(settings.get(PATH_KEY) or "").strip(),
        divisor=divisor,
        margin_bps=int(margin * 100) if margin else 0,
        quote_minutes=_bounded_int(settings.get(MINUTES_KEY), default=DEFAULT_MINUTES, low=MIN_MINUTES, high=MAX_MINUTES),
    )


def extract_number(payload: object, path: str) -> Decimal:
    """The number at a dotted path of a JSON document (list positions as digits), or RateUnavailable."""
    parts = [part for part in path.split(".") if part]
    if not parts or len(parts) > MAX_PATH_PARTS:
        raise RateUnavailable(UNAVAILABLE_TEXT)
    node = payload
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise RateUnavailable(UNAVAILABLE_TEXT)
    if isinstance(node, bool) or not isinstance(node, (int, float, str)):
        raise RateUnavailable(UNAVAILABLE_TEXT)
    try:
        value = Decimal(str(node).strip())
    except InvalidOperation as exc:
        raise RateUnavailable(UNAVAILABLE_TEXT) from exc
    if not value.is_finite() or value <= 0:
        raise RateUnavailable(UNAVAILABLE_TEXT)
    return value


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False, headers={"Accept": "application/json"}
        )
    return _client


async def fetch_api_rate(config: RateConfig) -> Decimal:
    """Read the rate from the shop's API: public https only, no redirects, bounded in size and time."""
    try:
        url, host = validate_public_https_url(config.api_url)
        await ensure_host_resolves_public(host)
    except UnsafeUrlError as exc:
        raise RateUnavailable(UNAVAILABLE_TEXT) from exc
    try:
        async with _http().stream("GET", url) as response:
            if response.status_code != 200:
                raise RateUnavailable(UNAVAILABLE_TEXT)
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise RateUnavailable(UNAVAILABLE_TEXT)
        payload = json.loads(body)
    except RateUnavailable:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise RateUnavailable(UNAVAILABLE_TEXT) from exc
    rate = extract_number(payload, config.api_path) / config.divisor
    if not 0 < rate <= MAX_RATE:
        raise RateUnavailable(UNAVAILABLE_TEXT)
    return rate


# scope (0 = platform) -> (rate, monotonic seconds when it was read)
_RATES: dict[int, tuple[Decimal, float]] = {}


def store_api_rate(scope: int, rate: Decimal, *, now: float | None = None) -> bool:
    """Remember an API rate unless it jumped implausibly from a recent one; True when stored."""
    moment = time.monotonic() if now is None else now
    previous = _RATES.get(scope)
    if previous and moment - previous[1] < JUMP_GUARD_SECONDS:
        if abs(rate - previous[0]) / previous[0] > MAX_JUMP_RATIO:
            logger.warning("crypto rate jump ignored scope=%s", scope)
            return False
    _RATES[scope] = (rate, moment)
    return True


def forget_rates() -> None:
    _RATES.clear()


def current_rate(config: RateConfig, scope: int, *, now: float | None = None) -> Decimal | None:
    """The rate to quote with: a fresh API rate in API mode, else the manual rate, else None."""
    moment = time.monotonic() if now is None else now
    if config.mode == MODE_API:
        cached = _RATES.get(scope)
        if cached and moment - cached[1] <= RATE_MAX_AGE_SECONDS:
            return cached[0]
    return config.manual_rate


def amount_micro(toman: int, rate: Decimal, margin_bps: int) -> int:
    """The crypto amount for a toman price, rounded up to two decimals so the customer never underpays."""
    units = Decimal(int(toman)) / rate * Decimal(10_000 + int(margin_bps)) / Decimal(10_000)
    micro = math.ceil(units * AMOUNT_SCALE)
    return -(-micro // DISPLAY_STEP) * DISPLAY_STEP


def format_amount(micro: int) -> str:
    return f"{Decimal(int(micro)) / AMOUNT_SCALE:.2f}"


def quote_for(
    config: RateConfig | None, *, scope: int, wallet_asset: str, toman: int, now: datetime | None = None
) -> CryptoQuote | None:
    """None when this shop does not convert this asset; RateUnavailable when it should but has no rate."""
    if config is None or str(wallet_asset or "").strip().upper() != config.asset:
        return None
    rate = current_rate(config, scope)
    if rate is None or int(toman) <= 0:
        raise RateUnavailable(UNAVAILABLE_TEXT)
    moment = now or datetime.now(timezone.utc)
    return CryptoQuote(
        asset=config.asset,
        amount_micro=amount_micro(toman, rate, config.margin_bps),
        rate_scaled=int(rate * RATE_SCALE),
        expires_at=moment + timedelta(minutes=config.quote_minutes),
    )


def quote_from_settings(ui: Mapping[str, str], *, wallet_asset: str, toman: int) -> CryptoQuote | None:
    """The quote for the current shop (from the bot's shop context) and a wallet's asset."""
    from app.services.users import current_shop_reseller_id

    return quote_for(
        config_from_settings(ui), scope=current_shop_reseller_id() or 0, wallet_asset=wallet_asset, toman=toman
    )


def display_amount(quote: CryptoQuote, toman_text: str) -> str:
    """What the customer is asked to pay: the crypto amount, with the toman price it stands for."""
    return f"{format_amount(quote.amount_micro)} {quote.asset} (معادل {toman_text})"


def validity_note(quote: CryptoQuote, *, now: datetime | None = None) -> str:
    minutes = max(1, math.ceil((quote.expires_at - (now or datetime.now(timezone.utc))).total_seconds() / 60))
    return f"\n⏳ این مبلغ تا {minutes} دقیقه معتبر است؛ بعد از آن ادمین ممکن است رسید را با نرخ جدید بررسی کند."


async def attach_quote(session: AsyncSession, payment_id: int, quote: CryptoQuote) -> None:
    """Save the quote on a payment that is still undecided."""
    await session.execute(
        update(Payment)
        .where(Payment.id == int(payment_id), Payment.status == PaymentStatus.PENDING.value)
        .values(
            crypto_asset=quote.asset,
            crypto_amount_micro=quote.amount_micro,
            crypto_rate_scaled=quote.rate_scaled,
            crypto_quote_expires_at=quote.expires_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


def quote_lines(payment: Payment, *, now: datetime | None = None) -> tuple[str, str, str] | None:
    """(amount, rate, validity) texts for staff reviewing a converted payment, or None when it has no quote."""
    amount_micro_saved = getattr(payment, "crypto_amount_micro", None)
    asset = getattr(payment, "crypto_asset", None)
    if not amount_micro_saved or not asset:
        return None
    amount = f"{format_amount(amount_micro_saved)} {asset}"
    rate = f"{Decimal(getattr(payment, 'crypto_rate_scaled', None) or 0) / RATE_SCALE:,.2f}"
    validity = ""
    expires = getattr(payment, "crypto_quote_expires_at", None)
    if expires is not None:
        aware = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        remaining = (aware - (now or datetime.now(timezone.utc))).total_seconds()
        validity = "منقضی شده" if remaining <= 0 else f"{math.ceil(remaining / 60)} دقیقه دیگر"
    return amount, rate, validity


REFRESH_CONCURRENCY = 5


async def refresh_api_rates(settings_for, shop_ids: list[int | None]) -> int:
    """Fetch the API rate of every shop in API mode (settings read here, fetches in parallel); returns how many stored."""
    import asyncio

    todo: list[tuple[int, RateConfig]] = []
    for shop_id in shop_ids:
        config = config_from_settings(await settings_for(shop_id))
        if config is not None and config.mode == MODE_API and config.api_url:
            todo.append((0 if shop_id is None else int(shop_id), config))
    gate = asyncio.Semaphore(REFRESH_CONCURRENCY)

    async def one(scope: int, config: RateConfig) -> bool:
        async with gate:
            try:
                return store_api_rate(scope, await fetch_api_rate(config))
            except RateUnavailable:
                logger.warning("crypto rate fetch failed scope=%s", scope)
                return False
            except Exception:
                logger.exception("crypto rate refresh failed scope=%s", scope)
                return False

    results = await asyncio.gather(*(one(scope, config) for scope, config in todo))
    return sum(results)
