"""Telegram Mini App (WebApp) initData validation and persona resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, ResellerProfile, Role
from app.services.miniapp_shop import (
    PLATFORM_SCOPE,
    MiniScope,
    assert_shop_member,
    assert_shop_route,
    is_shop_staff,
    mini_scope,
    request_scope,
)
from app.services.miniapp_registration import ensure_mini_user, referral_code_from_start_param, telegram_profile
from app.services.referral_registration import ReferralRequired
from app.services.shop_customer_block import BLOCKED_MESSAGE as SHOP_BLOCKED_MESSAGE, is_blocked_in_shop
from app.services.platform_identity import is_bot_platform_admin
from app.services.users import demote_stale_admin, set_shop_reseller_id


logger = logging.getLogger(__name__)

# Telegram initData is typically <2KiB; hard-cap stops oversized header DoS.
_MAX_INIT_DATA_CHARS = 8192
# Telegram signs initData once, when the Mini App opens, and never refreshes it while the
# window stays open (desktop users keep it open for hours). The signature, the blocked flag
# and shop membership are still checked on every request.
INIT_DATA_MAX_AGE_SEC = 24 * 3600
# auth_date comes from Telegram's clock; a later one only means this server's clock is behind.
INIT_DATA_MAX_FUTURE_SKEW_SEC = 600

# Sent in ``X-Mini-Error`` so the page can tell why it was turned away; never carries data.
ERROR_HEADER = "X-Mini-Error"
NO_INIT_DATA = "no_init_data"
BAD_SIGNATURE = "bad_signature"
EXPIRED = "expired"
# Steps the person can finish inside the Mini App (``/api/mini/onboarding``), as in the bot.
REFERRAL_REQUIRED = "referral_required"
FORCE_JOIN = "force_join"
TERMS = "terms"
BLOCKED = "blocked"

_REJECT_TEXT = {
    NO_INIT_DATA: "اطلاعات ورود تلگرام دریافت نشد؛ مینی‌اپ را از داخل تلگرام باز کنید",
    BAD_SIGNATURE: "اعتبار ورود تأیید نشد؛ مینی‌اپ را از دکمهٔ «Open» همین ربات باز کنید",
    EXPIRED: "نشست مینی‌اپ منقضی شده؛ مینی‌اپ را ببندید و دوباره باز کنید",
    REFERRAL_REQUIRED: "برای ثبت‌نام، کد معرف معتبر لازم است",
    FORCE_JOIN: "ابتدا در کانال‌های اجباری عضو شوید",
    TERMS: "ابتدا قوانین را بپذیرید",
    BLOCKED: "دسترسی شما مسدود شده است",
}


def mini_reject(status: int, code: str, *, text: str | None = None) -> HTTPException:
    """A refusal with Persian text and a machine-readable reason for the page."""
    return HTTPException(status, text or _REJECT_TEXT[code], headers={ERROR_HEADER: code})


def validate_webapp_init_data(init_data: str, *, bot_token: str | None = None) -> dict[str, Any]:
    """Validate Telegram WebApp ``initData`` HMAC and freshness; return user dict."""
    settings = get_settings()
    token = (bot_token or settings.bot_token or "").strip()
    if not token:
        raise HTTPException(503, "bot token missing")
    raw = (init_data or "").strip()
    if not raw or len(raw) > _MAX_INIT_DATA_CHARS:
        raise mini_reject(401, BAD_SIGNATURE)
    parsed = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise mini_reject(401, BAD_SIGNATURE)
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        # Usually a page opened from another bot's button (e.g. a shop bot on the platform page).
        logger.info("miniapp initData signature mismatch platform=%s", bot_token is None)
        raise mini_reject(401, BAD_SIGNATURE)
    _assert_fresh(parsed.get("auth_date"))
    try:
        user = json.loads(parsed.get("user", "{}") or "{}")
    except json.JSONDecodeError:
        raise mini_reject(401, BAD_SIGNATURE) from None
    if not isinstance(user, dict) or not user.get("id"):
        raise mini_reject(401, BAD_SIGNATURE)
    return user


def _assert_fresh(raw_auth_date: str | None) -> None:
    """Refuse initData older than the window, or dated ahead of this server's clock."""
    try:
        auth_date = int(raw_auth_date or "0")
    except (TypeError, ValueError):
        raise mini_reject(401, BAD_SIGNATURE) from None
    if auth_date <= 0:
        raise mini_reject(401, BAD_SIGNATURE)
    age = time.time() - auth_date
    if age < -INIT_DATA_MAX_FUTURE_SKEW_SEC:
        # Telegram's clock is right; a large negative age means this server's clock is behind.
        logger.warning("miniapp initData dated %.0fs ahead; check the server clock (NTP)", -age)
        raise mini_reject(401, EXPIRED)
    if age > INIT_DATA_MAX_AGE_SEC:
        raise mini_reject(401, EXPIRED)


def start_param_from_init_data(init_data: str) -> str:
    """The ``startapp`` parameter Telegram signed into initData, or "" when there is none.

    Read only after ``validate_webapp_init_data`` accepted the same string: the value is then
    covered by Telegram's signature and cannot be forged by the page.
    """
    return dict(parse_qsl(init_data or "", keep_blank_values=True)).get("start_param", "")


def init_data_from_request(request: Request) -> str:
    """Header only — never accept initData in query strings (logs/history leakage)."""
    init_data = request.headers.get("X-Telegram-Init-Data") or ""
    if not init_data.strip():
        raise mini_reject(401, NO_INIT_DATA)
    if len(init_data) > _MAX_INIT_DATA_CHARS:
        raise mini_reject(401, BAD_SIGNATURE)
    return init_data


def assert_miniapp_feature_enabled() -> None:
    """Fail closed when HTTPS / public URL is not ready (same gate as menu button)."""
    if not get_settings().miniapp_enabled:
        raise HTTPException(404, "مینی‌اپ فعال نیست")


async def load_mini_user(session: AsyncSession, request: Request) -> BotUser:
    """The verified person, once they passed force-join and the entry terms (parity with the bot).

    Each unfinished step is refused with its own ``X-Mini-Error`` code so the page can show
    that step (``/api/mini/onboarding``) instead of a dead end.
    """
    try:
        user = await load_mini_principal(session, request)
    except ReferralRequired:
        raise mini_reject(403, REFERRAL_REQUIRED) from None
    scope = mini_scope(request)
    await assert_mini_force_join(session, user, scope=scope)
    await assert_mini_terms_entry(session, user, scope=scope)
    return user


async def verified_telegram_id(session: AsyncSession, request: Request) -> int:
    """Who signed this request's initData, checked before anyone is registered (for rate limits)."""
    assert_miniapp_feature_enabled()
    set_shop_reseller_id(None)
    scope = await request_scope(session, request)
    tg_user = validate_webapp_init_data(init_data_from_request(request), bot_token=scope.bot_token or None)
    try:
        return telegram_profile(tg_user).telegram_id
    except ValueError:
        raise mini_reject(401, BAD_SIGNATURE) from None


async def load_mini_principal(
    session: AsyncSession, request: Request, *, referral_code: str | None = None
) -> BotUser:
    """Verify initData and load (or register) the person, without the onboarding gates.

    A shop's Mini App (``X-Mini-Shop``) is verified with that shop bot's token, admits only
    the shop's customers and staff, and reaches only the routes opened for shops; the
    verified scope is left on ``request.state.mini_scope``. Someone who never sent ``/start``
    is registered the way ``/start`` would; ``ReferralRequired`` is raised when this bot only
    admits people with a valid referral code. Blocked accounts are rejected.
    """
    assert_miniapp_feature_enabled()
    # Start every request on the platform: platform settings (the shop-Mini-App switch among
    # them) must never be read through a shop context left from an earlier request.
    set_shop_reseller_id(None)
    scope = await request_scope(session, request)
    # Before anyone is registered: a route a shop's Mini App may not reach joins nobody to the shop.
    assert_shop_route(scope, request)
    init_data = init_data_from_request(request)
    tg_user = validate_webapp_init_data(init_data, bot_token=scope.bot_token or None)
    code = referral_code or referral_code_from_start_param(start_param_from_init_data(init_data))
    try:
        profile = telegram_profile(tg_user)
    except ValueError:
        raise mini_reject(401, BAD_SIGNATURE) from None
    user = await ensure_mini_user(session, profile, scope=scope, referral_code=code)
    if bool(getattr(user, "is_blocked", False)):
        raise mini_reject(403, BLOCKED)
    if await demote_stale_admin(session, user):
        await session.commit()
    await assert_shop_member(session, scope, user)
    if scope.is_shop and not is_shop_staff(scope, user) and await is_blocked_in_shop(session, int(user.id), scope.shop_id):
        raise mini_reject(403, BLOCKED, text=SHOP_BLOCKED_MESSAGE)
    request.state.mini_scope = scope
    # The order, catalog and wallet services read the shop from the same context the shop
    # bot sets per update; it is set only after the shop and the person were verified.
    set_shop_reseller_id(scope.shop_id)
    return user


def resolve_mini_persona(user: BotUser) -> str:
    """Map BotUser → miniapp shell: admin | reseller | user."""
    if is_bot_platform_admin(user):
        return "admin"
    if (user.role or "").strip() == Role.RESELLER.value:
        return "reseller"
    return "user"


def is_gated_customer(user: BotUser, scope: MiniScope) -> bool:
    """Customers pass force-join and terms; staff (platform, or this shop's) do not."""
    if scope.is_shop:
        return not is_shop_staff(scope, user)
    return resolve_mini_persona(user) == "user"


@dataclass(frozen=True, slots=True)
class ForceJoinCheck:
    """The required channels of this Mini App's bot and which of them the person has not joined."""

    entries: tuple[dict[str, Any], ...]
    channels: tuple[str, ...]
    missing: tuple[str, ...]
    unverified: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.missing and not self.unverified


async def mini_force_join_check(
    session: AsyncSession, user: BotUser, *, scope: MiniScope = PLATFORM_SCOPE, fresh: bool = False
) -> ForceJoinCheck | None:
    """Membership of the required channels, or None when force-join does not apply to this person.

    Admin/reseller personas are not gated (same as Telegram middleware). In a shop's Mini
    App the shop's channels apply, checked through the shop's bot. ``fresh`` drops the short
    positive cache first, as the bot's «عضو شدم» button does.
    """
    if not is_gated_customer(user, scope):
        return None
    # Local import: the membership check still lives in the bot layer, which services must not load at import time.
    from aiogram import Bot

    from app.bot.middlewares import check_force_join_all, clear_force_join_member_cache
    from app.services.users import (
        get_all_settings,
        on,
        parse_force_join_channels,
        parse_force_join_entries,
    )

    ui = await get_all_settings(session, reseller_id=scope.shop_id)
    if not on(ui.get("force_join_enabled")):
        return None
    raw_channels = ui.get("force_join_channel")
    channels = parse_force_join_channels(raw_channels)
    if not channels:
        return None
    entries = parse_force_join_entries(raw_channels)
    token = (scope.bot_token or get_settings().bot_token or "").strip()
    if not token:
        raise HTTPException(503, "bot token missing")
    if fresh:
        clear_force_join_member_cache(int(user.telegram_id))
    bot = Bot(token=token)
    try:
        missing, unverified = await check_force_join_all(bot, int(user.telegram_id), channels, entries=entries)
    finally:
        await bot.session.close()
    return ForceJoinCheck(
        entries=tuple(entries),
        channels=tuple(channels),
        missing=tuple(missing),
        unverified=tuple(unverified),
    )


async def assert_mini_force_join(session: AsyncSession, user: BotUser, *, scope: MiniScope = PLATFORM_SCOPE) -> None:
    """Mirror bot ForceJoinMiddleware for the Mini App (users only)."""
    check = await mini_force_join_check(session, user, scope=scope)
    if check is not None and not check.passed:
        raise mini_reject(403, FORCE_JOIN)


async def assert_mini_terms_entry(session: AsyncSession, user: BotUser, *, scope: MiniScope = PLATFORM_SCOPE) -> None:
    """Mirror bot TermsEntryMiddleware for Mini App (users only); a shop's own terms in its Mini App."""
    if not is_gated_customer(user, scope):
        return
    from app.services.terms import needs_entry_gate
    from app.services.users import get_all_settings

    ui = await get_all_settings(session, reseller_id=scope.shop_id)
    prompt = await needs_entry_gate(
        session,
        user,
        ui,
        menu_role="user",
        reseller_owner_id=scope.shop_id,
    )
    if prompt is not None:
        raise mini_reject(403, TERMS)


async def load_reseller_profile(
    session: AsyncSession, user: BotUser
) -> ResellerProfile | None:
    if (user.role or "").strip() != Role.RESELLER.value:
        return None
    result = await session.execute(
        select(ResellerProfile).where(ResellerProfile.user_id == user.id)
    )
    return result.scalar_one_or_none()
