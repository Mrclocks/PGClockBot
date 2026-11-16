"""Mini App sign-up: a person who opens the Mini App before ``/start`` becomes a bot user.

Opening the Mini App from Telegram search, the bot's profile or a home-screen shortcut never
sends ``/start``. The signed initData already proves who the person is, so they are registered
the way ``/start`` registers them (the same ``get_or_create_user``, referral rules and shop
attribution) instead of being turned away.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.miniapp_shop import MiniScope, is_shop_staff
from app.services.referral_registration import normalize_referral_code
from app.services.shop_membership import is_shop_customer
from app.services.users import get_or_create_user, reset_shop_reseller_id, set_shop_reseller_id

logger = logging.getLogger(__name__)

# The same payload ``/start ref_<code>`` carries, sent as ``t.me/<bot>?startapp=ref_<code>``.
REFERRAL_START_PREFIX = "ref_"
USERNAME_MAX_CHARS = 64
FULL_NAME_MAX_CHARS = 255


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    """Who signed the initData, as Telegram describes them."""

    telegram_id: int
    username: str | None
    full_name: str | None


def _clean_text(value: Any, limit: int) -> str | None:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    return text[:limit] or None


def telegram_profile(tg_user: dict[str, Any]) -> TelegramProfile:
    """The person from a verified initData ``user`` object."""
    telegram_id = tg_user.get("id")
    if type(telegram_id) is not int or telegram_id <= 0:
        raise ValueError("initData user id is not a positive integer")
    first = _clean_text(tg_user.get("first_name"), FULL_NAME_MAX_CHARS) or ""
    last = _clean_text(tg_user.get("last_name"), FULL_NAME_MAX_CHARS) or ""
    return TelegramProfile(
        telegram_id=telegram_id,
        username=_clean_text(tg_user.get("username"), USERNAME_MAX_CHARS),
        full_name=_clean_text(f"{first} {last}", FULL_NAME_MAX_CHARS),
    )


def referral_code_from_start_param(start_param: str | None) -> str | None:
    """The referral code in ``ref_<code>``; any other start parameter carries none."""
    value = (start_param or "").strip()
    if not value.startswith(REFERRAL_START_PREFIX):
        return None
    return normalize_referral_code(value) or None


async def _first_visit_to_shop(session: AsyncSession, user: BotUser, scope: MiniScope) -> bool:
    """True when a known person opens a shop's Mini App before ever using that shop's bot."""
    if not scope.is_shop or is_shop_staff(scope, user) or bool(user.is_blocked):
        return False
    return not await is_shop_customer(session, user, scope.shop_id)


async def ensure_mini_user(
    session: AsyncSession,
    profile: TelegramProfile,
    *,
    scope: MiniScope,
    referral_code: str | None = None,
) -> BotUser:
    """The person's bot user, created or joined to this shop exactly as ``/start`` would.

    Raises ``ReferralRequired`` when this bot only admits people with a valid referral code
    and ``referral_code`` is missing or wrong. Two concurrent first opens create one row:
    ``get_or_create_user`` falls back to the existing row on the unique ``telegram_id``.
    """
    user = await session.scalar(select(BotUser).where(BotUser.telegram_id == profile.telegram_id))
    if user is not None and not await _first_visit_to_shop(session, user, scope):
        return user
    # The referral and registration settings are read in this shop, like an update to its bot.
    context = set_shop_reseller_id(scope.shop_id)
    try:
        registered = await get_or_create_user(
            session,
            profile.telegram_id,
            username=profile.username,
            full_name=profile.full_name,
            referred_by_code=referral_code if user is None else None,
            reseller_owner_id=scope.shop_id,
        )
    finally:
        reset_shop_reseller_id(context)
    if user is None:
        logger.info("miniapp registered bot user id=%s shop=%s", registered.id, scope.shop_id)
    return registered
