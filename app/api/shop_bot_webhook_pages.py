"""The shared webhook path that Telegram posts shop-bot updates to (see ``shop_bot_webhook``)."""

from __future__ import annotations

import json
import logging

from aiogram.types import Update
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.services.reseller_bots import get_reseller_bot_manager
from app.services.security_policy import WEBHOOK_MAX_BODY_BYTES, content_length_ok
from app.services.shop_bot_webhook import SECRET_HEADER, SHOP_WEBHOOK_PREFIX, ShopWebhookConfig

logger = logging.getLogger(__name__)

PROFILE_ID_MAX = 2147483647


def _reply(status_code: int) -> JSONResponse:
    return JSONResponse({"ok": status_code == 200}, status_code=status_code)


def register_shop_bot_webhook(app: FastAPI, *, config: ShopWebhookConfig) -> None:
    @app.post(SHOP_WEBHOOK_PREFIX + "/{profile_id}")
    async def shop_bot_webhook(profile_id: int, request: Request) -> JSONResponse:
        if not 1 <= profile_id <= PROFILE_ID_MAX or not config.accepts(profile_id, request.headers.get(SECRET_HEADER)):
            logger.warning("shop bot webhook rejected: bad secret profile=%s", profile_id if 1 <= profile_id <= PROFILE_ID_MAX else "-")
            return _reply(403)
        manager = get_reseller_bot_manager()
        bot = manager.webhook_bot(profile_id) if manager is not None else None
        if manager is None or bot is None:
            # Not running here right now (starting, restarting): Telegram retries later.
            return _reply(503)
        if not content_length_ok(request.headers.get("content-length"), WEBHOOK_MAX_BODY_BYTES):
            return _reply(413)
        body = await request.body()
        if len(body) > WEBHOOK_MAX_BODY_BYTES:
            return _reply(413)
        try:
            update = Update.model_validate(json.loads(body), context={"bot": bot})
        except (json.JSONDecodeError, ValidationError):
            return _reply(400)
        # Answer once queued, like the main bot: a slow handler must not hold Telegram's delivery.
        await manager.accept_webhook_update(bot, update)
        return _reply(200)
