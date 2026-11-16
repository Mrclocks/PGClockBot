"""The bot's «Main Mini App»: the «Open» button beside it in Telegram search and on its profile.

Telegram has no Bot API method to turn it on; the owner does it in BotFather. The panel shows
the address to paste there and whether Telegram reports it on (``getMe.has_main_web_app``).
The answer is cached briefly so opening the settings tab does not call Telegram every time.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.token import TokenValidationError

from app.config import get_settings

logger = logging.getLogger(__name__)

GET_ME_TIMEOUT_SEC = 5.0
# "On" rarely changes; "off" is re-read soon so the panel notices right after BotFather.
ON_TTL_SEC = 600.0
OFF_TTL_SEC = 30.0
CACHE_MAX_ENTRIES = 256
USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


@dataclass(frozen=True, slots=True)
class MainAppGuide:
    """What the BotFather guide card shows; ``enabled`` is None when Telegram could not be asked."""

    url: str
    bot_username: str
    enabled: bool | None

    @property
    def open_link(self) -> str:
        """Opens the Main Mini App from anywhere (the bot's chat when it is still off)."""
        return f"https://t.me/{self.bot_username}?startapp" if self.bot_username else ""


@dataclass(frozen=True, slots=True)
class _BotFacts:
    username: str
    has_main_web_app: bool


_cache: dict[str, tuple[float, _BotFacts]] = {}


def clean_bot_username(raw: str | None) -> str:
    """A Telegram bot username without «@», or "" when it is not one."""
    value = (raw or "").strip().lstrip("@")
    return value if USERNAME_PATTERN.fullmatch(value) else ""


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cached(key: str) -> _BotFacts | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, facts = hit
    ttl = ON_TTL_SEC if facts.has_main_web_app else OFF_TTL_SEC
    return facts if time.monotonic() - stored_at < ttl else None


def _remember(key: str, facts: _BotFacts) -> None:
    if len(_cache) >= CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]
    _cache[key] = (time.monotonic(), facts)


async def _bot_facts(token: str) -> _BotFacts | None:
    key = _cache_key(token)
    facts = _cached(key)
    if facts is not None:
        return facts
    try:
        bot = Bot(token=token)
    except TokenValidationError:
        logger.warning("main mini app: the saved bot token is malformed")
        return None
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=GET_ME_TIMEOUT_SEC)
    except (TelegramAPIError, asyncio.TimeoutError, OSError) as exc:
        # The type only: a transport error can carry the request URL, which holds the token.
        logger.warning("main mini app: getMe failed (%s)", type(exc).__name__)
        return None
    finally:
        await bot.session.close()
    facts = _BotFacts(username=clean_bot_username(me.username), has_main_web_app=bool(me.has_main_web_app))
    _remember(key, facts)
    return facts


async def main_app_guide(token: str, *, url: str, known_username: str | None = None) -> MainAppGuide:
    """The address to paste in BotFather for this bot and whether its Main Mini App is on."""
    token = (token or "").strip()
    facts = await _bot_facts(token) if token else None
    username = clean_bot_username(known_username) or (facts.username if facts else "")
    return MainAppGuide(
        url=url if url.startswith("https://") else "",
        bot_username=username,
        enabled=facts.has_main_web_app if facts else None,
    )


async def platform_main_app_guide() -> MainAppGuide:
    settings = get_settings()
    return await main_app_guide(settings.bot_token or "", url=settings.miniapp_url, known_username=settings.bot_username)


def platform_bot_username() -> str:
    """The platform bot's username for «open in Telegram», without calling Telegram (page hot path)."""
    settings = get_settings()
    configured = clean_bot_username(settings.bot_username)
    if configured or not settings.bot_token:
        return configured
    hit = _cache.get(_cache_key(settings.bot_token))
    return hit[1].username if hit else ""
