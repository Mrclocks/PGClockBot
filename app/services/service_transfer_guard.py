"""Facts about a service's transfers that other services check, without importing the transfer service.

A refund for a cancelled service goes to its current owner's wallet. After a transfer that owner
did not pay for the service, so a cancellation request of a transferred service is refused here
and left to staff, who can see who paid.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ServiceTransfer, UserService
from app.services.service_transfer_settings import STATUS_ACCEPTED

TRANSFERRED_REFUSAL = "این سرویس از مشتری دیگری منتقل شده و از این مسیر لغو نمی‌شود؛ با پشتیبانی تماس بگیرید"


async def was_transferred(session: AsyncSession, service_id: int | None) -> bool:
    if not service_id:
        return False
    found = await session.scalar(
        select(ServiceTransfer.id).where(
            ServiceTransfer.service_id == int(service_id), ServiceTransfer.status == STATUS_ACCEPTED
        ).limit(1)
    )
    return found is not None


async def assert_not_transferred(session: AsyncSession, service_id: int | None) -> None:
    if await was_transferred(session, service_id):
        raise ValueError(TRANSFERRED_REFUSAL)


async def order_service_was_transferred(session: AsyncSession, *, order_id: int, service_id: int | None) -> bool:
    """Whether any service minted for the order has since moved to another customer (one query)."""
    minted = select(UserService.id).where(
        or_(UserService.id == (int(service_id) if service_id else -1), UserService.remark == f"order:{int(order_id)}")
    ).scalar_subquery()
    found = await session.scalar(
        select(ServiceTransfer.id).where(
            ServiceTransfer.service_id.in_(minted), ServiceTransfer.status == STATUS_ACCEPTED
        ).limit(1)
    )
    return found is not None
