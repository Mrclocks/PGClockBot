"""Mini App change of a service's subscription link: what the sheet shows, and the change itself.

The service must be the Mini App user's and sold by this Mini App's shop; the rules, the limit and
the single revoke live in ``service_link_reset``, the same as in the bot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store, _owned_service_or_404, _require_commerce_ready, _scope_ui
from app.db.models import UserService
from app.services.db_safe import rollback_quiet
from app.services.miniapp_auth import load_mini_user
from app.services.miniapp_shop import mini_scope
from app.services.service_link_reset import changes_left, link_reset_button, start_link_reset
from app.services.service_link_reset_settings import STATUS_DONE, link_reset_policy

logger = logging.getLogger(__name__)

NOT_OFFERED = "تغییر لینک برای این سرویس فعال نیست"


def register_miniapp_link_reset(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/service/{service_id}/link-reset")
    async def mini_link_reset_offer(service_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        svc = await _owned_service_or_404(
            session, await session.get(UserService, service_id), user, shop_id=mini_scope(request).shop_id
        )
        ui = await _scope_ui(session, request)
        if not await link_reset_button(session, svc, ui):
            return _no_store({"offered": False, "left": 0})
        left = await changes_left(session, int(svc.id), link_reset_policy(ui), datetime.now(timezone.utc))
        return _no_store({"offered": True, "left": left})

    @app.post("/api/mini/service/{service_id}/link-reset")
    async def mini_link_reset(service_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        shop_id = mini_scope(request).shop_id
        await _owned_service_or_404(session, await session.get(UserService, service_id), user, shop_id=shop_id)
        try:
            outcome = await start_link_reset(
                session, user, service_id, shop_id=shop_id, ui=await _scope_ui(session, request)
            )
        except ValueError as exc:
            # The service raises only its own fixed Persian messages, never panel or database text.
            await rollback_quiet(session)
            raise HTTPException(400, str(exc)) from None
        done = outcome.status == STATUS_DONE and outcome.link is not None
        return _no_store({"status": outcome.status, "subscription_url": outcome.link.url if done else None})
