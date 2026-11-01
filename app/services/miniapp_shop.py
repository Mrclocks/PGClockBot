"""Which shop a Mini App request belongs to, and whether that shop's Mini App may answer.

The platform Mini App is unchanged: a request without a shop key is the platform's and is
verified with the main bot token. A shop's Mini App sends the random key from its address
(``/miniapp/s/<key>/``) in ``X-Mini-Shop``. The key only names the shop; the request is then
verified with that shop bot's own token, so a changed key fails the signature check. The
shop is never taken from a request body or query string, and an unknown or switched-off
shop is refused, never answered as the platform.

A shop's Mini App answers only when the platform switch, the shop's ``miniapp`` permission
and the shop's own switch are all on, and only on the routes in ``SHOP_ROUTES``. That list
grows one phase at a time, after each route reads its data from ``MiniScope.shop_id``.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, ResellerProfile
from app.services.reseller_access import parse_telegram_ids
from app.services.resellers import has_perm
from app.services.secret_box import reveal_bot_token
from app.services.shop_membership import is_shop_customer
from app.services.users import get_setting, on, set_setting

SHOP_KEY_HEADER = "X-Mini-Shop"
PLATFORM_SWITCH = "miniapp_shops_enabled"
SHOP_SWITCH = "miniapp_shop_enabled"
PERMISSION = "miniapp"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
KEY_BYTES = 16
# Route paths (as registered) a shop's Mini App may call. A route is listed only after it reads
# its data from MiniScope.shop_id; every other route stays closed to shops.
SHOP_ROUTES: frozenset[str] = frozenset({
    "/api/mini/me",
    "/api/mini/services",
    "/api/mini/service/{service_id}",
    "/api/mini/service/{service_id}/qr",
    "/api/mini/activity",
    "/api/mini/guides",
    "/api/mini/guides/{guide_id}/send",
    "/api/mini/plans",
    "/api/mini/buy",
    "/api/mini/service/{service_id}/renewal-preview",
    "/api/mini/renew",
    "/api/mini/service/{service_id}/addons",
    "/api/mini/addon",
    "/api/mini/topup",
    "/api/mini/topup/{payment_id}/receipt",
    "/api/mini/topup/check",
    "/api/mini/support",
    "/api/mini/support/tickets/{ticket_id}",
    "/api/mini/support/tickets",
    "/api/mini/support/tickets/{ticket_id}/messages",
    "/api/mini/checkout",
    "/api/mini/checkout/{order_id}/pay",
    "/api/mini/checkout/{order_id}/cancel",
    "/api/mini/wallet/redeem",
    "/api/mini/club",
    "/api/mini/club/history",
    "/api/mini/club/rewards/{reward_id}/redeem",
})
# What a shop's Mini App shows so far; the page hides every other section.
SHOP_SECTIONS = ("services", "guides", "activity", "shop", "topup", "support", "codes", "club")
SHOP_NAME_SETTING = "bot_tg_name"
DEFAULT_SHOP_NAME = "فروشگاه"

SHOP_UNAVAILABLE = "مینی‌اپ این فروشگاه فعال نیست"
ROUTE_UNAVAILABLE = "این بخش هنوز در مینی‌اپ این فروشگاه نیست"
NOT_A_CUSTOMER = "شما مشتری این فروشگاه نیستید؛ ابتدا ربات فروشگاه را استارت کنید"


@dataclass(frozen=True, slots=True)
class MiniScope:
    """The shop a Mini App request was verified for; ``shop_id`` None is the platform."""

    shop_id: int | None = None
    profile_id: int | None = None
    bot_admin_telegram_ids: frozenset[int] = frozenset()
    # Verifies the request; never logged or returned.
    bot_token: str = field(default="", repr=False)

    @property
    def is_shop(self) -> bool:
        return self.shop_id is not None


PLATFORM_SCOPE = MiniScope()


def new_shop_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def shop_key_from_request(request: Request) -> str | None:
    """The shop key the page sent, or None for the platform Mini App; a malformed key is refused."""
    raw = request.headers.get(SHOP_KEY_HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not KEY_PATTERN.fullmatch(key):
        raise HTTPException(404, SHOP_UNAVAILABLE)
    return key


async def shop_miniapp_open(session: AsyncSession, profile: ResellerProfile) -> bool:
    """All three switches on, and the shop is active with a set-up bot."""
    if not profile.is_active or profile.setup_completed_at is None or not profile.bot_token:
        return False
    if not on(await get_setting(session, PLATFORM_SWITCH, "0")):
        return False
    if not has_perm(profile, PERMISSION):
        return False
    return on(await get_setting(session, SHOP_SWITCH, "0", reseller_id=int(profile.user_id)))


async def resolve_shop_scope(session: AsyncSession, key: str) -> MiniScope:
    """The open shop named by ``key``; anything else is the same 404, so keys cannot be probed."""
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.miniapp_key == key))
    if profile is None or not await shop_miniapp_open(session, profile):
        raise HTTPException(404, SHOP_UNAVAILABLE)
    token = reveal_bot_token(profile.bot_token or "")
    if not token:
        raise HTTPException(404, SHOP_UNAVAILABLE)
    return MiniScope(
        shop_id=int(profile.user_id),
        profile_id=int(profile.id),
        bot_admin_telegram_ids=frozenset(parse_telegram_ids(profile.bot_admin_ids)),
        bot_token=token,
    )


async def request_scope(session: AsyncSession, request: Request) -> MiniScope:
    key = shop_key_from_request(request)
    return PLATFORM_SCOPE if key is None else await resolve_shop_scope(session, key)


def is_shop_staff(scope: MiniScope, user: BotUser) -> bool:
    return scope.is_shop and (
        int(user.id) == scope.shop_id or int(user.telegram_id or 0) in scope.bot_admin_telegram_ids
    )


async def assert_shop_member(session: AsyncSession, scope: MiniScope, user: BotUser) -> None:
    """Only the shop's own customers and its owner or bot admins use its Mini App."""
    if not scope.is_shop or is_shop_staff(scope, user):
        return
    if not await is_shop_customer(session, user, scope.shop_id):
        raise HTTPException(403, NOT_A_CUSTOMER)


def assert_shop_route(scope: MiniScope, request: Request) -> None:
    """A shop's Mini App reaches only the routes opened for shops so far."""
    if not scope.is_shop:
        return
    route = request.scope.get("route")
    if getattr(route, "path", None) not in SHOP_ROUTES:
        raise HTTPException(404, ROUTE_UNAVAILABLE)


def shop_nav() -> list[dict[str, str]]:
    return [
        {"id": "home", "label": "خانه", "icon": "home"},
        {"id": "services", "label": "سرویس‌ها", "icon": "svc"},
        {"id": "shop", "label": "خرید", "icon": "shop"},
        {"id": "account", "label": "حساب", "icon": "account"},
    ]


async def shop_display_name(session: AsyncSession, scope: MiniScope) -> str:
    """The name the shop gave its bot, else the bot's username."""
    name = (await get_setting(session, SHOP_NAME_SETTING, "", reseller_id=scope.shop_id)).strip()
    if name:
        return name[:64]
    profile = await session.get(ResellerProfile, scope.profile_id) if scope.profile_id else None
    return ((profile.bot_username if profile else "") or DEFAULT_SHOP_NAME)[:64]


async def ensure_shop_key(session: AsyncSession, profile: ResellerProfile) -> str:
    """The shop's Mini App key, created on first use (the caller commits)."""
    if not profile.miniapp_key:
        profile.miniapp_key = new_shop_key()
        await session.flush()
    return str(profile.miniapp_key)


def shop_miniapp_url(key: str) -> str:
    """The shop's Mini App address, or "" while the panel has no HTTPS address."""
    base = get_settings().miniapp_url
    return f"{base}s/{key}/" if base and key else ""


async def shop_menu_url(session: AsyncSession, profile: ResellerProfile) -> str:
    """Where the shop bot's «Open» button points, or "" when its Mini App is off."""
    if not profile.miniapp_key or not await shop_miniapp_open(session, profile):
        return ""
    return shop_miniapp_url(profile.miniapp_key)


async def shop_miniapp_available(session: AsyncSession, profile: ResellerProfile) -> bool:
    """The shop may turn its Mini App on: platform switch and its permission (its own switch aside)."""
    return (
        bool(profile.is_active and profile.setup_completed_at is not None and profile.bot_token)
        and on(await get_setting(session, PLATFORM_SWITCH, "0"))
        and has_perm(profile, PERMISSION)
    )


# Where a shop's Mini App stands, from the first missing condition to open.
STATE_SETUP = "setup"
STATE_PLATFORM_OFF = "platform_off"
STATE_NO_PERMISSION = "no_permission"
STATE_SHOP_OFF = "shop_off"
STATE_OPEN = "open"

# What the shop owner reads in their own settings: what to do next.
_SHOP_BLOCKER_TEXT = {
    STATE_SETUP: "ابتدا راه‌اندازی ربات فروشگاه را کامل کنید",
    STATE_PLATFORM_OFF: "مینی‌اپ فروشگاه‌ها هنوز از طرف مدیریت پلتفرم روشن نشده است؛ با مدیریت هماهنگ کنید",
    STATE_NO_PERMISSION: "دسترسی «مینی‌اپ» برای فروشگاه شما داده نشده است؛ از مدیریت بخواهید آن را فعال کند",
}


async def shop_miniapp_state(session: AsyncSession, profile: ResellerProfile) -> str:
    """The first condition that keeps the shop's Mini App closed, or ``STATE_OPEN``."""
    if not (profile.is_active and profile.setup_completed_at is not None and profile.bot_token):
        return STATE_SETUP
    if not on(await get_setting(session, PLATFORM_SWITCH, "0")):
        return STATE_PLATFORM_OFF
    if not has_perm(profile, PERMISSION):
        return STATE_NO_PERMISSION
    if not on(await get_setting(session, SHOP_SWITCH, "0", reseller_id=int(profile.user_id))):
        return STATE_SHOP_OFF
    return STATE_OPEN


async def shop_miniapp_blocker(session: AsyncSession, profile: ResellerProfile) -> str:
    """Why the shop cannot turn its Mini App on, in words it can act on; "" when it can."""
    return _SHOP_BLOCKER_TEXT.get(await shop_miniapp_state(session, profile), "")


async def set_shop_miniapp(session: AsyncSession, profile: ResellerProfile, *, enabled: bool) -> None:
    """Turn the shop's own switch on or off; on needs the platform switch and the permission."""
    if enabled and not await shop_miniapp_available(session, profile):
        raise ValueError("مینی‌اپ برای این فروشگاه فعال نشده است؛ با مدیریت هماهنگ کنید")
    if enabled:
        await ensure_shop_key(session, profile)
    await set_setting(session, SHOP_SWITCH, "1" if enabled else "0", reseller_id=int(profile.user_id))
    await session.commit()


async def set_platform_shop_miniapps(session: AsyncSession, *, enabled: bool) -> bool:
    """The platform switch for every shop's Mini App; True when it changed."""
    wanted = "1" if enabled else "0"
    current = "1" if on(await get_setting(session, PLATFORM_SWITCH, "0")) else "0"
    if current == wanted:
        return False
    await set_setting(session, PLATFORM_SWITCH, wanted)
    return True


def switch_value(form: Mapping[str, Any], key: str) -> str:
    """A settings checkbox: an unticked box posts nothing and means off."""
    return "1" if str(form.get(key) or "") == "1" else "0"


def mini_scope(request: Request) -> MiniScope:
    """The scope ``load_mini_user`` verified for this request (the platform before it ran)."""
    scope = getattr(request.state, "mini_scope", None)
    return scope if isinstance(scope, MiniScope) else PLATFORM_SCOPE
