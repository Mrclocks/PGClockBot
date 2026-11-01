"""Telegram Mini App (WebApp) initData validation and persona resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
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
    request_scope,
)
from app.services.shop_customer_block import BLOCKED_MESSAGE as SHOP_BLOCKED_MESSAGE, is_blocked_in_shop
from app.services.platform_identity import is_bot_platform_admin
from app.services.users import set_shop_reseller_id


# Telegram initData is typically <2KiB; hard-cap stops oversized header DoS.
_MAX_INIT_DATA_CHARS = 8192


def validate_webapp_init_data(init_data: str, *, bot_token: str | None = None) -> dict[str, Any]:
    """Validate Telegram WebApp ``initData`` HMAC and freshness; return user dict."""
    settings = get_settings()
    token = (bot_token or settings.bot_token or "").strip()
    if not token:
        raise HTTPException(503, "bot token missing")
    raw = (init_data or "").strip()
    if not raw or len(raw) > _MAX_INIT_DATA_CHARS:
        raise HTTPException(401, "bad initData")
    parsed = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "missing hash")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise HTTPException(401, "bad initData")
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise HTTPException(401, "bad auth_date")
    now = time.time()
    # Reject future skew (>5m) and stale initData (>10m)
    if auth_date <= 0 or auth_date > now + 300 or now - auth_date > 600:
        raise HTTPException(401, "expired")
    try:
        user = json.loads(parsed.get("user", "{}") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(401, "bad user")
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(401, "bad user")
    return user


def init_data_from_request(request: Request) -> str:
    """Header only — never accept initData in query strings (logs/history leakage)."""
    init_data = request.headers.get("X-Telegram-Init-Data") or ""
    if not init_data.strip():
        raise HTTPException(401, "no initData")
    if len(init_data) > _MAX_INIT_DATA_CHARS:
        raise HTTPException(401, "bad initData")
    return init_data


def assert_miniapp_feature_enabled() -> None:
    """Fail closed when HTTPS / public URL is not ready (same gate as menu button)."""
    if not get_settings().miniapp_enabled:
        raise HTTPException(404, "مینی‌اپ فعال نیست")


async def load_mini_user(session: AsyncSession, request: Request) -> BotUser:
    """Validate initData and load the BotUser row (must have /start'd).

    A shop's Mini App (``X-Mini-Shop``) is verified with that shop bot's token, admits only
    the shop's customers and staff, and reaches only the routes opened for shops; the
    verified scope is left on ``request.state.mini_scope``. Blocked accounts are rejected;
    end-users must pass force-join and the terms (parity with the bot's middlewares).
    """
    assert_miniapp_feature_enabled()
    # Start every request on the platform: platform settings (the shop-Mini-App switch among
    # them) must never be read through a shop context left from an earlier request.
    set_shop_reseller_id(None)
    scope = await request_scope(session, request)
    tg_user = validate_webapp_init_data(init_data_from_request(request), bot_token=scope.bot_token or None)
    tg_id = tg_user.get("id")
    result = await session.execute(select(BotUser).where(BotUser.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "start the bot first")
    if bool(getattr(user, "is_blocked", False)):
        raise HTTPException(403, "دسترسی شما مسدود شده است")
    await assert_shop_member(session, scope, user)
    if scope.is_shop and not is_shop_staff(scope, user) and await is_blocked_in_shop(session, int(user.id), scope.shop_id):
        raise HTTPException(403, SHOP_BLOCKED_MESSAGE)
    assert_shop_route(scope, request)
    # Parity with bot: users cannot use the app until channel membership is confirmed.
    await assert_mini_force_join(session, user, scope=scope)
    await assert_mini_terms_entry(session, user, scope=scope)
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


def _is_gated_customer(user: BotUser, scope: MiniScope) -> bool:
    """Customers pass force-join and terms; staff (platform, or this shop's) do not."""
    if scope.is_shop:
        return not is_shop_staff(scope, user)
    return resolve_mini_persona(user) == "user"


async def assert_mini_force_join(session: AsyncSession, user: BotUser, *, scope: MiniScope = PLATFORM_SCOPE) -> None:
    """Mirror bot ForceJoinMiddleware for Mini App commerce (users only).

    Admin/reseller personas are not gated (same as Telegram middleware). In a shop's Mini
    App the shop's channels apply, checked through the shop's bot.
    """
    if not _is_gated_customer(user, scope):
        return
    from aiogram import Bot

    from app.bot.middlewares import check_force_join_all
    from app.services.users import (
        get_all_settings,
        on,
        parse_force_join_channels,
        parse_force_join_entries,
    )

    ui = await get_all_settings(session, reseller_id=scope.shop_id)
    if not on(ui.get("force_join_enabled")):
        return
    raw_channels = ui.get("force_join_channel")
    channels = parse_force_join_channels(raw_channels)
    if not channels:
        return
    entries = parse_force_join_entries(raw_channels)
    token = (scope.bot_token or get_settings().bot_token or "").strip()
    if not token:
        raise HTTPException(503, "bot token missing")
    bot = Bot(token=token)
    try:
        missing, unverified = await check_force_join_all(
            bot,
            int(user.telegram_id),
            channels,
            entries=entries,
        )
    finally:
        await bot.session.close()
    if missing or unverified:
        raise HTTPException(
            403,
            "ابتدا در کانال‌های اجباری عضو شوید و از ربات عضویت را تأیید کنید",
        )



async def assert_mini_terms_entry(session: AsyncSession, user: BotUser, *, scope: MiniScope = PLATFORM_SCOPE) -> None:
    """Mirror bot TermsEntryMiddleware for Mini App (users only); a shop's own terms in its Mini App."""
    if not _is_gated_customer(user, scope):
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
        from fastapi import HTTPException

        raise HTTPException(
            403,
            "ابتدا قوانین ربات را در تلگرام بپذیرید",
        )

async def load_reseller_profile(
    session: AsyncSession, user: BotUser
) -> ResellerProfile | None:
    if (user.role or "").strip() != Role.RESELLER.value:
        return None
    result = await session.execute(
        select(ResellerProfile).where(ResellerProfile.user_id == user.id)
    )
    return result.scalar_one_or_none()
