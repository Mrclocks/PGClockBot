"""Mini App club: points, tier, invite link, rewards and points history.

Thin routes over ``app.services.customer_loyalty``. The customer comes from initData and
the club from the verified Mini App scope: a shop's Mini App reaches only that shop's club.
"""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store, _require_commerce_ready
from app.config import get_settings
from app.services.customer_loyalty import ClubError, club_summary, points_history_page, redeem_club_reward
from app.db.models import ResellerProfile
from app.services.miniapp_auth import load_mini_user
from app.services.miniapp_shop import MiniScope, mini_scope
from app.services.redact import user_safe_error
from app.services.reseller_bots import open_notify_bot_for_user

logger = logging.getLogger(__name__)

_ID_MAX = 2**31 - 1
_bot_username_cache: list[str] = []


def _valid_id(value: object) -> int:
    if type(value) is not int or not 0 < value <= _ID_MAX:
        raise HTTPException(400, "شناسه نامعتبر است")
    return value


async def _platform_bot_username(session: AsyncSession, user) -> str:
    """The main bot's @username for invite links; asked from Telegram once when not configured."""
    configured = (get_settings().bot_username or "").strip().lstrip("@")
    if configured:
        return configured
    if _bot_username_cache:
        return _bot_username_cache[0]
    bot, should_close = await open_notify_bot_for_user(session, user, shop_id=None)
    try:
        username = ((await bot.get_me()).username or "").strip()
    except TelegramAPIError:
        logger.warning("mini app club could not read the bot username", exc_info=True)
        return ""
    finally:
        if should_close:
            await bot.session.close()
    if username:
        _bot_username_cache[:] = [username]
    return username


async def _club_bot_username(session: AsyncSession, user, scope: MiniScope) -> str:
    """The @username invite links point to: the shop's own bot in a shop, the main bot otherwise."""
    if not scope.is_shop:
        return await _platform_bot_username(session, user)
    profile = await session.get(ResellerProfile, scope.profile_id)
    return str(profile.bot_username or "").strip().lstrip("@") if profile is not None else ""


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "درخواست نامعتبر است") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "درخواست نامعتبر است")
    return body


def register_miniapp_loyalty(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/club")
    async def mini_club(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        scope = mini_scope(request)
        username = await _club_bot_username(session, user, scope)
        return _no_store({"club": await club_summary(session, user, shop_id=scope.shop_id, bot_username=username)})

    @app.get("/api/mini/club/history")
    async def mini_club_history(request: Request, before: int = 0, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        if not 0 <= before <= _ID_MAX:
            raise HTTPException(400, "شناسه نامعتبر است")
        items, has_more = await points_history_page(
            session, int(user.id), shop_id=mini_scope(request).shop_id, before_id=before or None
        )
        return _no_store({"items": items, "has_more": has_more})

    @app.post("/api/mini/club/rewards/{reward_id}/redeem")
    async def mini_club_redeem(reward_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        user_id = int(user.id)
        body = await _json_object(request)
        raw_service_id = body.get("service_id")
        reward_id = _valid_id(reward_id)
        service_id = None if raw_service_id is None else _valid_id(raw_service_id)
        try:
            result = await redeem_club_reward(
                session, user, shop_id=mini_scope(request).shop_id, reward_id=reward_id, service_id=service_id
            )
        except ClubError as exc:
            raise HTTPException(400, user_safe_error(exc, fallback="بازخرید ناموفق بود")) from None
        except Exception:
            # The redemption rolled back as a whole: no points were spent and nothing was granted.
            logger.exception("mini app club redeem failed user=%s reward=%s", user_id, reward_id)
            raise HTTPException(500, "بازخرید ناموفق بود؛ دوباره تلاش کنید") from None
        return _no_store({**result, "message": "جایزه با موفقیت دریافت شد"})
