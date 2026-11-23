"""Telegram links to a shop's bot: deep links, the bot username and the chat URL."""

from __future__ import annotations

import re
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_bot_username(bot_username: str | None) -> str | None:
    """Strip ``@`` and accept only Telegram username shape."""
    uname = (bot_username or "").strip().lstrip("@")
    if not uname or not _BOT_USERNAME_RE.fullmatch(uname):
        return None
    return uname


def bot_deep_link(bot_username: str | None, payload: str) -> str | None:
    uname = (bot_username or "").strip().lstrip("@")
    if not uname:
        return None
    payload = (payload or "").strip()
    if not payload:
        return f"https://t.me/{uname}"
    return f"https://t.me/{uname}?start={quote(payload)}"


def bot_tg_resolve_link(bot_username: str | None) -> str | None:
    """Native deep link that opens the bot chat inside the Telegram client.

    Prefer this over ``https://t.me/...`` on payment-return pages: Telegram's
    in-app browser often swallows ``https://t.me`` navigations (treats them as
    internal URIs without opening the chat), and ``t.me`` HTTPS may be filtered.
    """
    uname = normalize_bot_username(bot_username)
    if not uname:
        return None
    return f"tg://resolve?domain={uname}"


async def resolve_shop_bot_username(
    session: AsyncSession,
    *,
    shop_owner_id: int | None,
) -> str | None:
    """Shop ``ResellerProfile.bot_username``, else platform ``BOT_USERNAME``."""
    from app.config import get_settings
    from app.db.models import ResellerProfile

    uname: str | None = None
    rid = int(shop_owner_id) if shop_owner_id is not None else 0
    if rid > 0:
        profile = (
            await session.execute(
                select(ResellerProfile).where(ResellerProfile.user_id == rid)
            )
        ).scalar_one_or_none()
        uname = (profile.bot_username if profile else None) or None
    if not uname:
        uname = (get_settings().bot_username or "").strip() or None
    stripped = (uname or "").strip().lstrip("@") or None
    return normalize_bot_username(stripped) or stripped


async def shop_bot_chat_url(
    session: AsyncSession,
    *,
    shop_owner_id: int | None,
) -> str | None:
    """``tg://resolve?domain=<bot>`` for returning from a payment WebView."""
    uname = await resolve_shop_bot_username(session, shop_owner_id=shop_owner_id)
    return bot_tg_resolve_link(uname) or bot_deep_link(uname, "")


async def shop_bot_https_url(
    session: AsyncSession,
    *,
    shop_owner_id: int | None,
) -> str | None:
    """HTTPS ``t.me`` fallback when the native ``tg://`` scheme is unavailable."""
    return bot_deep_link(
        await resolve_shop_bot_username(session, shop_owner_id=shop_owner_id),
        "",
    )
