"""Keep the subscription link saved on customers' services equal to the panel's after it changes.

The customer's service card reads the panel through the saved token and «🔗 لینک و QR» shows the
saved link, so a link revoked on the panel without updating the rows leaves the customer a dead
link and an error card. Every path that revokes a link stores the new one through this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import Integer, String, Text, column, update, values
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserService
from app.services.db_safe import rollback_quiet
from app.services.pasarguard import PasarGuardError, extract_sub_token, user_subscription_url
from app.services.shop_scope import resolve_shop_scope_id

logger = logging.getLogger(__name__)

PANEL_READ_TIMEOUT_SECONDS = 8.0
PANEL_READ_ERRORS = (asyncio.TimeoutError, PasarGuardError, httpx.HTTPError)


@dataclass(frozen=True, slots=True)
class PanelLink:
    url: str = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LinkScope:
    """Which shop's services may be updated; ``every_shop`` is only for the platform owner."""

    shop_id: int | None
    every_shop: bool = False

    @classmethod
    def for_owner(cls, owner_id: int | None) -> LinkScope:
        """The acting shop's services; None is the platform owner, who acts on every shop."""
        return cls(shop_id=None, every_shop=True) if owner_id is None else cls(shop_id=int(owner_id))


def staff_link_scope(staff: dict) -> LinkScope:
    """``LinkScope.for_owner`` for a web staff session; a scopeless session raises ``ShopScopeError``."""
    return LinkScope.for_owner(resolve_shop_scope_id(staff))


def panel_link(user: object) -> PanelLink | None:
    """The subscription link and its token from a panel user record, or None when it has none."""
    url = user_subscription_url(user if isinstance(user, dict) else None)
    token = extract_sub_token(url)
    return PanelLink(url, token) if url and token else None


async def read_panel_link(pg: Any, pg_user_id: int) -> PanelLink | None:
    """Read the user from the panel, bypassing the read cache, and return its current link."""
    user = await asyncio.wait_for(
        pg.get_user_by_id(int(pg_user_id), use_cache=False), timeout=PANEL_READ_TIMEOUT_SECONDS
    )
    return panel_link(user)


def _scope_clause(scope: LinkScope):
    if scope.every_shop:
        return None
    if scope.shop_id is None:
        return UserService.shop_id.is_(None)
    return UserService.shop_id == int(scope.shop_id)


async def save_links(session: AsyncSession, links: Mapping[int, PanelLink], *, scope: LinkScope) -> int:
    """Write each panel user's link to its services in one statement; returns the rows changed."""
    if not links:
        return 0
    rows = values(
        column("pg_user_id", Integer), column("url", Text), column("token", String(255)), name="new_links"
    ).data([(int(pg_id), link.url, link.token) for pg_id, link in links.items()])
    statement = (
        update(UserService)
        .where(UserService.pg_user_id == rows.c.pg_user_id)
        .values(subscription_url=rows.c.url, subscription_token=rows.c.token)
        .execution_options(synchronize_session=False)
    )
    clause = _scope_clause(scope)
    if clause is not None:
        statement = statement.where(clause)
    result = await session.execute(statement)
    return int(result.rowcount or 0)


async def link_after_revoke(pg: Any, pg_user_id: int, revoke_response: object) -> PanelLink | None:
    """The new link from the revoke answer, or read from the panel when the answer has none.

    A failed read is logged and returns None: the revoke already happened and must not be repeated.
    """
    link = panel_link(revoke_response)
    if link is not None:
        return link
    try:
        return await read_panel_link(pg, pg_user_id)
    except PANEL_READ_ERRORS:
        logger.warning("new subscription link not read after revoke pg_user=%s", pg_user_id, exc_info=True)
        return None


async def commit_links(session: AsyncSession | None, links: Mapping[int, PanelLink], *, scope: LinkScope) -> int:
    """Save and commit the new links of revokes that already happened on the panel.

    The revoke cannot be undone, so a failed write is logged and rolled back instead of raised.
    """
    if session is None or not links:
        return 0
    try:
        changed = await save_links(session, links, scope=scope)
        await session.commit()
    except SQLAlchemyError:
        logger.warning("new subscription links not saved pg_users=%s", sorted(links), exc_info=True)
        await rollback_quiet(session)
        return 0
    return changed


async def store_revoked_link(
    session: AsyncSession | None, pg: Any, *, pg_user_id: int, revoke_response: object, scope: LinkScope
) -> PanelLink | None:
    """After a revoke on the panel, save the new link on the user's services and commit."""
    link = await link_after_revoke(pg, pg_user_id, revoke_response)
    if link is not None:
        await commit_links(session, {int(pg_user_id): link}, scope=scope)
    return link
