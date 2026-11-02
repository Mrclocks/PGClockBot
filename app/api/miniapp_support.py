"""Mini App support: contacts, the customer's tickets, opening a ticket and replying.

Thin routes over ``app.services.customer_support``; the customer comes from initData,
the support desk from the verified Mini App scope (a shop's Mini App writes to that shop,
alerted through its own bot), and every ticket id is re-checked against both.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store, _require_commerce, _scope_ui
from app.db.models import BotUser, Ticket
from app.services.customer_support import (
    SupportActor,
    SupportError,
    customer_ticket_thread,
    list_customer_tickets,
    notify_staff,
    open_customer_ticket,
    reply_customer_ticket,
    support_overview,
)
from app.services.miniapp_auth import load_mini_user
from app.services.miniapp_shop import mini_scope
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user

logger = logging.getLogger(__name__)

_ID_MAX = 2**31 - 1


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "درخواست نامعتبر است") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "درخواست نامعتبر است")
    return body


def _ticket_id(value: int) -> int:
    if not 0 < value <= _ID_MAX:
        raise HTTPException(404, "تیکت پیدا نشد")
    return value


async def _alert_staff(session: AsyncSession, user: BotUser, actor: SupportActor, ticket: Ticket, body: str | None) -> None:
    try:
        bot, should_close = await open_notify_bot_for_user(session, user, shop_id=actor.shop_id)
    except ShopBotUnavailable:
        # The ticket is saved and listed for the shop's staff; only the alert is lost.
        logger.warning("mini app ticket alert: shop bot unavailable shop=%s", actor.shop_id, exc_info=True)
        return
    try:
        await notify_staff(bot, session, actor, ticket, body=body)
    finally:
        if should_close:
            await bot.session.close()


def register_miniapp_support(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/support")
    async def mini_support(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        ui = await _scope_ui(session, request)
        tickets = await list_customer_tickets(session, int(user.id), shop_id=mini_scope(request).shop_id)
        return _no_store({**support_overview(ui), "tickets": tickets})

    @app.get("/api/mini/support/tickets/{ticket_id}")
    async def mini_support_ticket(ticket_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        try:
            thread = await customer_ticket_thread(
                session, int(user.id), _ticket_id(ticket_id), shop_id=mini_scope(request).shop_id
            )
        except SupportError as exc:
            raise HTTPException(404, str(exc)) from None
        return _no_store({"ticket": thread})

    @app.post("/api/mini/support/tickets")
    async def mini_support_open(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        actor = SupportActor.of(user, shop_id=mini_scope(request).shop_id)
        body = await _json_object(request)
        try:
            ticket = await open_customer_ticket(session, actor, subject=body.get("subject"), body=body.get("body"))
        except SupportError as exc:
            raise HTTPException(400, str(exc)) from None
        await _alert_staff(session, user, actor, ticket, None)
        return _no_store({"ticket_id": int(ticket.id), "message": f"تیکت #{int(ticket.id)} ثبت شد"})

    @app.post("/api/mini/support/tickets/{ticket_id}/messages")
    async def mini_support_reply(ticket_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        actor = SupportActor.of(user, shop_id=mini_scope(request).shop_id)
        payload = await _json_object(request)
        try:
            ticket = await reply_customer_ticket(session, actor, _ticket_id(ticket_id), body=payload.get("body"))
        except SupportError as exc:
            raise HTTPException(400, str(exc)) from None
        await _alert_staff(session, user, actor, ticket, str(payload.get("body") or "").strip())
        return _no_store({"ticket_id": int(ticket.id), "message": "پیام ثبت شد"})
