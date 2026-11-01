"""Canonical ownership checks and explicit legacy channel boundaries."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.customer_models import Customer
from app.db.models import BotUser, UserService

LEGACY_CHANNEL_MESSAGE = "این سرویس از مسیر وب‌سایت مدیریت می‌شود."


def is_legacy_service(service: UserService) -> bool:
    return service.bot_user_id is not None and service.integration_id is None


def require_legacy_service(service: UserService) -> None:
    if not is_legacy_service(service):
        raise ValueError(LEGACY_CHANNEL_MESSAGE)


@dataclass(frozen=True, slots=True)
class OwnershipCoverage:
    missing_adapters: int
    invalid_services: int

    @property
    def is_complete(self) -> bool:
        return self.missing_adapters == 0 and self.invalid_services == 0


async def ownership_coverage(session: AsyncSession) -> OwnershipCoverage:
    """Aggregate coverage without loading customer data or changing ownership."""
    missing = await session.scalar(select(func.count()).select_from(BotUser).outerjoin(
        Customer, Customer.legacy_bot_user_id == BotUser.id,
    ).where(Customer.id.is_(None)))
    invalid = await session.scalar(select(func.count()).select_from(UserService).outerjoin(
        Customer, Customer.id == UserService.customer_id,
    ).where(UserService.integration_id.is_(None), or_(
        UserService.bot_user_id.is_(None), Customer.id.is_(None),
        Customer.legacy_bot_user_id.is_(None), Customer.legacy_bot_user_id != UserService.bot_user_id,
        UserService.public_id.is_(None),
    )))
    return OwnershipCoverage(int(missing or 0), int(invalid or 0))


def legacy_service_clause() -> ColumnElement[bool]:
    """Keep legacy readers and claims closed to mismatched canonical ownership."""
    return (UserService.integration_id.is_(None) & UserService.bot_user_id.is_not(None) & (
        UserService.customer_id.is_(None) | select(Customer.id).where(
            Customer.id == UserService.customer_id, Customer.legacy_bot_user_id == UserService.bot_user_id,
        ).exists()
    ))


async def canonical_legacy_owner_matches(session: AsyncSession, service: UserService) -> bool:
    if not is_legacy_service(service):
        return False
    if service.customer_id is None:
        # The expansion release remains readable while its explicit backfill is running.
        return True
    return bool(await session.scalar(select(Customer.id).where(
        Customer.id == service.customer_id, Customer.legacy_bot_user_id == service.bot_user_id,
    )))
