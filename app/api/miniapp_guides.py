"""Mini App connection guides: the platform's customer guides, and sending one to the chat.

Thin routes over ``app.services.guide_delivery``. Only enabled platform guides for
customers are listed; the full guide (photos and videos) goes to the caller's own chat.
"""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store
from app.services.connection_guides import get_connection_guides, guides_for_audience
from app.services.guide_delivery import claim_guide_send_slot, find_customer_guide, guide_cards, send_guide_to_chat
from app.services.miniapp_auth import load_mini_user
from app.services.miniapp_shop import mini_scope
from app.services.reseller_bots import open_notify_bot_for_user

logger = logging.getLogger(__name__)


async def _customer_guides(session: AsyncSession, shop_id: int | None) -> list[dict]:
    """The customer guides of this Mini App's shop (None = platform)."""
    return guides_for_audience(await get_connection_guides(session, reseller_id=shop_id), "user")


def register_miniapp_guides(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/guides")
    async def mini_guides(request: Request, session: AsyncSession = Depends(get_db)):
        await load_mini_user(session, request)
        return _no_store({"guides": guide_cards(await _customer_guides(session, mini_scope(request).shop_id))})

    @app.post("/api/mini/guides/{guide_id}/send")
    async def mini_guide_send(guide_id: str, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        user_id, chat_id = int(user.id), int(user.telegram_id)
        shop_id = mini_scope(request).shop_id
        guides = await _customer_guides(session, shop_id)
        guide = find_customer_guide(guides, guide_id)
        if guide is None:
            raise HTTPException(404, "آموزش پیدا نشد")
        if not claim_guide_send_slot(user_id):
            raise HTTPException(429, "این آموزش به‌تازگی فرستاده شد؛ یک دقیقه بعد دوباره تلاش کنید")
        bot, should_close = await open_notify_bot_for_user(session, user, shop_id=shop_id)
        try:
            await send_guide_to_chat(session, bot, chat_id=chat_id, guide=guide, all_guides=guides, shop_id=shop_id)
        except TelegramAPIError:
            logger.warning("mini app guide send failed user=%s guide=%s", user_id, guide_id, exc_info=True)
            raise HTTPException(400, "ارسال ناموفق بود؛ مطمئن شوید ربات را در تلگرام Start کرده‌اید") from None
        finally:
            if should_close:
                await bot.session.close()
        return _no_store({"ok": True, "message": "آموزش در چت ربات فرستاده شد"})
