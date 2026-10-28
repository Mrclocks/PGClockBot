"""Explicit website customer presentation under the owning panel principal."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer, IntegrationCustomer
from app.db.models import UserService
from app.services.authz import can_shop
from app.services.integrations import managed_integration
from app.services.integration_policy import IntegrationError

PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class WebsiteCustomerRow:
    id: str
    display_name: str
    state: str
    service_count: int


async def website_customer_page(
    session: AsyncSession, staff: dict, integration_id: str, *, after: int = 0,
) -> tuple[tuple[WebsiteCustomerRow, ...], int | None]:
    integration, owner = await managed_integration(session, staff, integration_id)
    if not can_shop(owner.context, "integration_customers_read"):
        raise IntegrationError("permission_denied")
    if type(after) is not int or not 0 <= after <= 2**31 - 1:
        raise IntegrationError("invalid_request")
    count = select(func.count()).select_from(UserService).where(
        UserService.integration_id == integration.id, UserService.customer_id == Customer.id,
        UserService.shop_id == owner.shop.shop_id, UserService.owner_principal_id == owner.principal_id,
        UserService.shop_needs_review.is_(False),
    ).correlate(Customer).scalar_subquery()
    rows = (await session.execute(select(
        IntegrationCustomer.id, Customer.public_id, IntegrationCustomer.display_name, Customer.state, count,
    ).join(Customer, Customer.id == IntegrationCustomer.customer_id).where(
        IntegrationCustomer.integration_id == integration.id, IntegrationCustomer.id > after,
        Customer.legacy_bot_user_id.is_(None),
    ).order_by(IntegrationCustomer.id).limit(PAGE_SIZE + 1))).all()
    visible = rows[:PAGE_SIZE]
    return tuple(WebsiteCustomerRow(*row[1:]) for row in visible), visible[-1][0] if len(rows) > PAGE_SIZE else None
