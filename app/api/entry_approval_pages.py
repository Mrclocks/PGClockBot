"""JSON endpoints behind the «تأیید ورود کاربران جدید» list in the limits settings tab."""

from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram.exceptions import TelegramAPIError
from fastapi import Depends, FastAPI, Path
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.entry_approval import (
    VIEW_CLOSED,
    VIEW_PENDING,
    Action,
    Decision,
    approval_scope,
    decide,
    list_requests,
    staff_label,
)
from app.services.entry_approval_notify import settle_admin_cards, tell_customer
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user

logger = logging.getLogger(__name__)

REQUESTS_PATH = "/entry-approvals"
VIEWS = frozenset({VIEW_PENDING, VIEW_CLOSED})
ACTIONS = {action.value: action for action in Action}
MAX_ID = 2**31 - 1


def register_entry_approval_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.get(REQUESTS_PATH)
    async def entry_approval_list(
        view: str = VIEW_PENDING,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        try:
            shop_key = approval_scope(staff)
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        if view not in VIEWS:
            view = VIEW_PENDING
        return JSONResponse({"ok": True, "view": view, "requests": await list_requests(session, shop_key, view=view)})

    @app.post(REQUESTS_PATH + "/{approval_id}/{action}")
    async def entry_approval_decide(
        action: str,
        approval_id: int = Path(ge=1, le=MAX_ID),
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        try:
            shop_key = approval_scope(staff)
        except PermissionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        chosen = ACTIONS.get(action)
        if chosen is None:
            return JSONResponse({"ok": False, "error": "عملیات نامعتبر است"}, status_code=404)
        label = staff_label(username=staff.get("username"), user_id=staff.get("bot_user_id"), role=staff.get("role"))
        try:
            decision = await decide(session, approval_id, shop_key=shop_key, action=chosen, decided_by=label)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        await _after_decision(session, decision, actor=str(staff.get("username") or "پنل وب"))
        return JSONResponse({"ok": True, "status": decision.status})


async def _after_decision(session: AsyncSession, decision: Decision, *, actor: str) -> None:
    """Tell the customer, then mark the admins' Telegram cards; neither can undo the decision."""
    await tell_customer(session, decision)
    user = await session.get(BotUser, decision.user_id)
    if user is None:
        return
    try:
        bot, should_close = await open_notify_bot_for_user(session, user, shop_id=decision.shop_key or None)
    except ShopBotUnavailable:
        logger.warning("entry approval: shop bot unavailable shop=%s", decision.shop_key)
        return
    try:
        await settle_admin_cards(session, bot, decision, actor=actor)
    except TelegramAPIError:
        logger.warning("entry approval: settling admin cards failed approval=%s", decision.approval_id, exc_info=True)
    finally:
        if should_close:
            await bot.session.close()
