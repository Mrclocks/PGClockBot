"""One-time redirect from a shop-owner credentials card to the web login page."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reseller_panel_open import TOKEN_MAX_LEN, consume_panel_open_token
from app.services.resellers import get_reseller_panel_base_url

logger = logging.getLogger(__name__)


def register_reseller_panel_open(app: FastAPI, *, get_db) -> None:
    @app.get("/r/panel-open/{token}")
    async def panel_open_once(token: str, session: AsyncSession = Depends(get_db)):
        if not token or len(token) > TOKEN_MAX_LEN:
            raise HTTPException(404, "لینک نامعتبر یا منقضی است")
        shop_user_id = await consume_panel_open_token(session, token)
        if shop_user_id is None:
            # Never log the token value.
            logger.info("panel_open rejected")
            raise HTTPException(404, "لینک نامعتبر یا منقضی است")
        base = (await get_reseller_panel_base_url(session) or "").rstrip("/")
        if not base:
            raise HTTPException(404, "لینک نامعتبر یا منقضی است")
        return RedirectResponse(url=f"{base}/login", status_code=303)
