"""Shop ownership of a customer's services.

A service belongs to the shop that sold it (``UserService.shop_id``, written when it is
created), not to whoever the customer is attributed to today. Every staff action and every
customer-facing service view re-checks this where the service is read or changed.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import UserService

SERVICE_OUTSIDE_SHOP_MESSAGE = "این سرویس متعلق به فروشگاه دیگری است و از اینجا قابل مدیریت نیست"
_OWNER_UNKNOWN_MESSAGE = "مالکیت فروشگاه سرویس مشخص نیست"


async def service_shop_id(session: AsyncSession, service: UserService) -> int | None:
    """The shop that sold the service (None = platform); a service under review has none.

    ``session`` stays in the signature for the existing call sites.
    """
    if service.shop_needs_review:
        raise ValueError(_OWNER_UNKNOWN_MESSAGE)
    return service.shop_id


async def assert_service_in_shop(
    session: AsyncSession, service: UserService, shop_id: int | None
) -> None:
    """Raise ``ValueError`` unless the service was sold by ``shop_id`` (None = platform).

    An unresolvable owner fails closed with the helper's own error.
    """
    if await service_shop_id(session, service) != shop_id:
        raise ValueError(SERVICE_OUTSIDE_SHOP_MESSAGE)


async def customer_may_use_service(
    session: AsyncSession, service: UserService | None, user_id: int, shop_id: int | None
) -> bool:
    """True when the service is this customer's and was sold by the bot they are using."""
    if service is None or int(service.bot_user_id) != int(user_id):
        return False
    try:
        return await service_shop_id(session, service) == shop_id
    except ValueError:
        return False


def shop_services_clause(shop_id: int | None) -> ColumnElement[bool]:
    """Services ``shop_id`` sold and that are not waiting for an owner decision."""
    sold = UserService.shop_id.is_(None) if not shop_id else UserService.shop_id == int(shop_id)
    return sold & UserService.shop_needs_review.is_(False)


async def list_customer_services(
    session: AsyncSession, user_id: int, shop_id: int | None, *, limit: int | None = None
) -> list[UserService]:
    """The customer's services sold by ``shop_id``, newest first."""
    query = (
        select(UserService)
        .where(UserService.bot_user_id == int(user_id), shop_services_clause(shop_id))
        .order_by(UserService.id.desc())
    )
    if limit:
        query = query.limit(limit)
    return list(await session.scalars(query))


SERVICE_SEARCH_MAX_LENGTH = 64


def _service_name_clause(text: str) -> ColumnElement[bool]:
    """Case-insensitive "name contains" filter; rejects empty or oversized search text."""
    needle = (text or "").strip().lower()
    if not needle or len(needle) > SERVICE_SEARCH_MAX_LENGTH:
        raise ValueError("بخشی از نام سرویس را بفرستید (حداکثر ۶۴ حرف).")
    return func.lower(UserService.pg_username).contains(needle, autoescape=True)


async def search_customer_services(
    session: AsyncSession, user_id: int, shop_id: int | None, text: str, *, limit: int
) -> list[UserService]:
    """The customer's services in ``shop_id`` whose name contains ``text`` (any letter case)."""
    query = (
        select(UserService)
        .where(
            UserService.bot_user_id == int(user_id),
            shop_services_clause(shop_id),
            _service_name_clause(text),
        )
        .order_by(UserService.id.desc())
        .limit(limit)
    )
    return list(await session.scalars(query))


async def page_customer_services(
    session: AsyncSession,
    user_id: int,
    shop_id: int | None,
    *,
    limit: int,
    before_id: int | None = None,
    text: str | None = None,
) -> tuple[list[UserService], bool]:
    """One newest-first keyset page of the customer's services in ``shop_id``.

    Returns the page and whether older services remain. ``text`` narrows by name.
    """
    query = select(UserService).where(
        UserService.bot_user_id == int(user_id), shop_services_clause(shop_id)
    )
    if before_id is not None:
        query = query.where(UserService.id < int(before_id))
    if text is not None and text.strip():
        query = query.where(_service_name_clause(text))
    rows = list(await session.scalars(query.order_by(UserService.id.desc()).limit(limit + 1)))
    return rows[:limit], len(rows) > limit
