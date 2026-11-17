"""Quick links: ``t.me`` deep links staff send to a customer to open one part of the shop's bot.

The bot answers ``/start <payload>`` for these payloads (see ``bot/handlers/start.py``). A link always
points at the bot of the shop that owns what it opens, so a customer lands in the shop that can serve
them. Telegram accepts at most 64 characters from ``A-Z a-z 0-9 _ -`` as a start payload, so a gift
code that does not fit gets no link instead of a broken one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ResellerProfile
from app.services import home_overview
from app.services.authz import authz_from_staff, can_shop
from app.services.secret_box import reveal_bot_token
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.ux20 import bot_deep_link, normalize_bot_username

# (payload, label) in the order the settings page and the bot list them.
QUICK_LINKS: tuple[tuple[str, str], ...] = (
    ("renew", "تمدید"),
    ("wallet", "کیف پول"),
    ("support", "پشتیبانی"),
    ("config", "دریافت مجدد کانفیگ"),
    ("gift", "کد هدیه"),
)
START_PAYLOAD_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
PLATFORM_LINKS_PAGE = "/settings?tab=links"
SHOP_LINKS_PAGE = "/shop-settings?tab=links"


@dataclass(frozen=True, slots=True)
class QuickLink:
    key: str
    label: str
    url: str | None


async def shop_bot_username(session: AsyncSession, shop_id: int | None) -> str | None:
    """Username of the bot that serves ``shop_id`` (None = platform), from storage or a cached getMe."""
    if shop_id is None:
        stored = normalize_bot_username(get_settings().bot_username)
        if stored:
            return stored
        probe = await home_overview.check_bot_connection(get_settings().bot_token)
        return normalize_bot_username(probe.get("username"))
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    if profile is None:
        return None
    if profile.bot_username:
        return normalize_bot_username(profile.bot_username)
    token = reveal_bot_token(profile.bot_token)
    if not token:
        return None
    # Shop tokens are stored sealed; Telegram needs the plain token.
    probe = await home_overview.check_bot_connection(token)
    return normalize_bot_username(probe.get("username"))


def start_link(bot_username: str | None, payload: str) -> str | None:
    """A deep link for ``payload``, or None when Telegram would not accept the payload."""
    if not START_PAYLOAD_RE.fullmatch(payload or ""):
        return None
    return bot_deep_link(bot_username, payload)


def quick_links(bot_username: str | None) -> list[QuickLink]:
    return [QuickLink(key, label, start_link(bot_username, key)) for key, label in QUICK_LINKS]


def service_renew_link(bot_username: str | None, service_id: int) -> str | None:
    """Opens the renewal of one service; the bot checks the service belongs to whoever taps it."""
    return start_link(bot_username, f"renew_{int(service_id)}")


async def service_renew_links(session: AsyncSession, services: list[Any]) -> dict[int, str | None]:
    """Service id -> renewal link through the bot of the shop that sold it; one lookup per shop."""
    usernames: dict[int | None, str | None] = {}
    links: dict[int, str | None] = {}
    for service in services:
        shop = int(service.shop_id) if service.shop_id else None
        if shop not in usernames:
            usernames[shop] = await shop_bot_username(session, shop)
        links[int(service.id)] = service_renew_link(usernames[shop], int(service.id))
    return links


def gift_code_link(bot_username: str | None, code: str) -> str | None:
    """Redeems ``code`` for whoever taps it; None for a blank code."""
    clean = (code or "").strip()
    return start_link(bot_username, f"gift_{clean}") if clean else None


def staff_links_shop(staff: dict) -> int | None:
    """The shop whose bot a staff member's links point at (None = platform)."""
    return None if is_platform_admin(staff) else shop_owner_id(staff)


def quick_links_page(staff: dict | None) -> str | None:
    """The settings page with the quick links, when this staff member may open it."""
    if not staff:
        return None
    if is_platform_admin(staff):
        return PLATFORM_LINKS_PAGE
    return SHOP_LINKS_PAGE if can_shop(authz_from_staff(staff), "shop_settings") else None


async def build_magic_links_context(session: AsyncSession, staff: dict) -> dict[str, Any]:
    """Username and links for the settings «لینک‌های سریع» tab."""
    bot_username = await shop_bot_username(session, staff_links_shop(staff))
    links = quick_links(bot_username)
    return {
        "bot_username": bot_username,
        "quick_links": links,
        "links": {link.key: link.url for link in links},
    }
