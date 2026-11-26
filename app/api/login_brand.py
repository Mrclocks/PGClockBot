"""The platform brand on the sign-in page.

Before anyone signs in, the login page shows the panel name, logo and colour the platform Owner set in the web
panel settings. A shop's brand is never used here: whose shop the person works for is only known after sign-in.
The brand is read through ``panel_brand.load_brand`` (cached for a few seconds), and any database failure falls
back to the product default so the login page always renders.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.panel_brand import DEFAULT_BRAND, PanelBrand, load_brand

logger = logging.getLogger(__name__)

LOGIN_PATHS = frozenset({"/login"})

SessionFactory = Callable[[], AsyncSession]


async def platform_brand(session_factory: SessionFactory) -> PanelBrand:
    """The platform's brand, or the default when it cannot be read."""
    try:
        async with session_factory() as session:
            return await load_brand(session, None)
    except (SQLAlchemyError, OSError):
        logger.warning("login brand not readable; showing the default", exc_info=True)
        return DEFAULT_BRAND


def register_login_brand(app: FastAPI, *, session_factory: SessionFactory) -> None:
    """Attach the platform brand to sign-in requests; ``render`` then shows it like the panel header."""

    @app.middleware("http")
    async def login_brand(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path in LOGIN_PATHS:
            request.state.panel_brand = await platform_brand(session_factory)
        return await call_next(request)
