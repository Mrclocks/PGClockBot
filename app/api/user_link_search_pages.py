"""«یافتن با لینک اشتراک» on the users page: find a customer from a subscription link staff paste.

The link is a bearer secret, so it travels only in a POST body: never in a URL, the access log, a
flash message or the page. A match leads to the customer's row through their Telegram id; an
unknown link and a link sold by another shop get the same answer.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
from app.services.subscription_lookup import find_service_by_link
from app.services.users_ops import scoped_users_where

LINK_SEARCH_PATH = "/users/find-by-link"
LINK_FIELD = "link"
# A subscription URL is far shorter; anything longer is not one and is not parsed.
LINK_MAX_LENGTH = 2048
NOT_FOUND_TEXT = "سرویسی با این لینک در این فروشگاه پیدا نشد"


def _users_redirect(**params: str | int) -> RedirectResponse:
    return RedirectResponse(f"/users?{urlencode(params)}", status_code=303)


async def find_customer_by_link(session: AsyncSession, raw: str, *, shop_id: int | None) -> BotUser | None:
    """The customer in ``shop_id``'s user list who owns the service behind ``raw``, else None."""
    if len(raw) > LINK_MAX_LENGTH:
        return None
    match = await find_service_by_link(session, raw, shop_id=shop_id)
    if match is None:
        return None
    return await session.scalar(
        select(BotUser).where(BotUser.id == int(match.owner.id), scoped_users_where(shop_id)).limit(1)
    )


async def link_search_redirect(session: AsyncSession, raw: str, *, shop_id: int | None) -> RedirectResponse:
    """Where a link search leads: the customer's row, or the same «not found» for every miss."""
    customer = await find_customer_by_link(session, raw, shop_id=shop_id) if raw else None
    if customer is None:
        return _users_redirect(err=NOT_FOUND_TEXT)
    if customer.telegram_id is None:
        return _users_redirect(uid=int(customer.id))
    return _users_redirect(q=int(customer.telegram_id), uid=int(customer.id))


def register_user_link_search_pages(app: FastAPI, *, require_perm: Callable, get_db: Callable) -> None:
    @app.post(LINK_SEARCH_PATH)
    async def users_find_by_link(
        request: Request,
        staff: dict = Depends(require_perm("dashboard")),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            shop_id = resolve_shop_scope_id(staff)
        except ShopScopeError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        return await link_search_redirect(session, str(form.get(LINK_FIELD) or "").strip(), shop_id=shop_id)
