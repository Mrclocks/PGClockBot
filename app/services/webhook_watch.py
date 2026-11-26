"""Keep Telegram pointed at the address this process actually serves, and check a new bot token first.

Telegram stores one delivery setting per bot: a webhook URL, or none (then the bot polls). It drifts
when the domain moves, when a second install sets its own webhook with the same token, or when a
manual ``setWebhook`` is forgotten. ``check_webhook`` compares what Telegram has with what this process
serves and, when they differ, puts it back: the webhook URL again (pending updates are kept), or
no webhook for a polling bot. It never switches the delivery mode, because the webhook route only
exists when the process started in webhook mode; that change still needs a restart.

The address rules (Telegram's ports, following a domain change) live in ``webhook_address``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ResellerProfile
from app.services.home_overview import check_bot_connection
from app.services.numbers import to_fa_digits
from app.services.secret_box import hash_bot_token
from app.services.setup_wizard import telegram_webhook_endpoint
from app.services.webhook_secret import ensure_webhook_secret

logger = logging.getLogger(__name__)

WEBHOOK_CHECK_MINUTES = 5
# A delivery error newer than this is reported; older ones were already followed by successes or alerts.
RECENT_ERROR_SECONDS = 15 * 60
ALERT_REPEAT_SECONDS = 60 * 60
TELEGRAM_TIMEOUT_SEC = 10
_TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")

MODE_WEBHOOK = "webhook"
MODE_POLLING = "polling"
# Results of one check.
OK = "ok"
REPAIRED = "repaired"
REMOVED = "removed"
DELIVERY_ERROR = "delivery_error"
RESTART_NEEDED = "restart_needed"
UNREACHABLE = "unreachable"
ALERT_RESULTS = frozenset({REPAIRED, REMOVED, DELIVERY_ERROR, RESTART_NEEDED})

RESULT_TEXT = {
    OK: "تلگرام پیام‌ها را به همین سرور می‌فرستد.",
    REPAIRED: "آدرس Webhook در تلگرام با این سرور یکی نبود و دوباره تنظیم شد.",
    REMOVED: "روی این ربات Webhook دیگری تنظیم شده بود که دریافت پیام با Polling را متوقف می‌کرد؛ برداشته شد.",
    DELIVERY_ERROR: "تلگرام در رساندن پیام به Webhook خطا گرفته است؛ دامنه، گواهی و پورت را بررسی کنید.",
    RESTART_NEEDED: "روش یا مسیر دریافت پیام در تنظیمات عوض شده؛ برای اعمال، سرویس را ری‌استارت کنید.",
    UNREACHABLE: "اتصال به تلگرام برقرار نشد؛ وضعیت Webhook بررسی نشد.",
}

TOKEN_FORMAT_INVALID = "قالب توکن درست نیست؛ توکن کامل را از @BotFather کپی کنید."
TOKEN_REFUSED = "تلگرام این توکن را نپذیرفت؛ توکن قبلی دست نخورد."
TOKEN_OF_A_SHOP = "این توکن مال ربات یکی از فروشگاه‌هاست و نمی‌تواند ربات اصلی باشد."


@dataclass(frozen=True, slots=True)
class WebhookHealth:
    """One comparison of Telegram's delivery setting with this process."""

    result: str
    mode: str
    expected_url: str
    telegram_url: str
    pending_updates: int
    checked_at: datetime

    @property
    def text(self) -> str:
        return RESULT_TEXT.get(self.result, "")

    @property
    def pending_text(self) -> str:
        return to_fa_digits(self.pending_updates)

    @property
    def is_ok(self) -> bool:
        return self.result in {OK, REPAIRED, REMOVED}


@dataclass(slots=True)
class _ServedMode:
    """What this process listens on: set once at startup by ``app.main``."""

    mode: str = ""
    path: str = ""


_served = _ServedMode()
_last: WebhookHealth | None = None
_last_alert: dict[str, float] = {}


def mark_served_webhook(path: str) -> None:
    """Startup registered the webhook route at ``path`` and Telegram accepted it."""
    _served.mode, _served.path = MODE_WEBHOOK, path


def mark_served_polling() -> None:
    _served.mode, _served.path = MODE_POLLING, ""


def last_webhook_health() -> WebhookHealth | None:
    """The newest check, for the settings page; ``None`` before the first one."""
    return _last


def expected_webhook_url() -> tuple[str, str]:
    """(full URL, path) the current settings ask for; empty URL in polling mode."""
    settings = get_settings()
    _base, path, full = telegram_webhook_endpoint(settings.webhook_url, settings.webhook_path)
    return full, path


def _secret() -> str:
    return (get_settings().webhook_secret_token or "").strip() or ensure_webhook_secret()


def _recent_error(info: object, now: float) -> bool:
    when = getattr(info, "last_error_date", None)
    if when is None:
        return False
    stamp = when.timestamp() if isinstance(when, datetime) else float(when)
    return now - stamp <= RECENT_ERROR_SECONDS


async def _compare(bot: Bot, info: object, expected: str, path: str) -> str:
    """Bring Telegram back in line with what this process serves; returns the result code."""
    current = (getattr(info, "url", "") or "").strip()
    if _served.mode == MODE_POLLING:
        if not current:
            return OK
        await bot.delete_webhook(drop_pending_updates=False, request_timeout=TELEGRAM_TIMEOUT_SEC)
        return REMOVED
    if not expected or path != _served.path:
        return RESTART_NEEDED
    if current != expected:
        await bot.set_webhook(
            expected, secret_token=_secret(), drop_pending_updates=False, request_timeout=TELEGRAM_TIMEOUT_SEC
        )
        return REPAIRED
    return DELIVERY_ERROR if _recent_error(info, time.time()) else OK


async def check_webhook(bot: Bot) -> WebhookHealth | None:
    """Compare and repair once; ``None`` while startup has not said what this process serves."""
    global _last
    if not _served.mode:
        return None
    expected, path = expected_webhook_url()
    info = None
    try:
        info = await bot.get_webhook_info(request_timeout=TELEGRAM_TIMEOUT_SEC)
        result = await _compare(bot, info, expected, path)
    except (TelegramAPIError, TimeoutError, OSError):
        logger.warning("webhook check: Telegram call failed", exc_info=True)
        result = UNREACHABLE
    _last = WebhookHealth(
        result=result,
        mode=_served.mode,
        expected_url=expected if _served.mode == MODE_WEBHOOK else "",
        telegram_url=(getattr(info, "url", "") or "") if info is not None else "",
        pending_updates=int(getattr(info, "pending_update_count", 0) or 0) if info is not None else 0,
        checked_at=datetime.now(timezone.utc),
    )
    if result != OK:
        logger.warning("webhook check: %s (mode=%s)", result, _served.mode)
    return _last


def should_alert(health: WebhookHealth, *, now: float | None = None) -> bool:
    """True for a result staff must hear about, at most once an hour per kind of result."""
    if health.result not in ALERT_RESULTS:
        return False
    moment = time.monotonic() if now is None else now
    last = _last_alert.get(health.result)
    if last is not None and moment - last < ALERT_REPEAT_SECONDS:
        return False
    _last_alert[health.result] = moment
    return True


def alert_text(health: WebhookHealth) -> str:
    return f"⚠️ <b>دریافت پیام ربات</b>\n{health.text}"


async def verify_main_bot_token(session: AsyncSession, token: str) -> str:
    """The bot username of a new main-bot ``token``; ``ValueError`` (safe to show) when it must not be used.

    Checked before the token is written, so a typo can no longer restart the service into a dead bot.
    """
    token = (token or "").strip()
    if not _TOKEN_RE.match(token):
        raise ValueError(TOKEN_FORMAT_INVALID)
    shop = await session.scalar(
        select(ResellerProfile.id).where(ResellerProfile.bot_token_hash == hash_bot_token(token)).limit(1)
    )
    if shop is not None:
        raise ValueError(TOKEN_OF_A_SHOP)
    probe = await check_bot_connection(token)
    if not probe.get("ok") or not probe.get("username"):
        raise ValueError(TOKEN_REFUSED)
    return str(probe["username"])
