"""Customer registration and reads under fresh credential and fixed integration authority."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer, IntegrationCustomer
from app.db.customer_link_models import CustomerTelegramLink
from app.db.integration_models import Integration
from app.services.integration_authority import IntegrationAuthority, resolve_integration_authority
from app.services.integration_credentials import verify_credential
from app.services.integration_policy import IntegrationError, utc_time
from app.services.integration_transactions import finish_integration_write

REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)
CUSTOMER_ID_PATTERN = re.compile(r"cus_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII)


@dataclass(frozen=True, slots=True)
class CustomerInput:
    external_customer_ref: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class CustomerView:
    id: str
    external_customer_ref: str
    display_name: str
    state: str
    telegram_linked: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CustomerRegistration:
    customer: CustomerView
    created: bool


def validate_customer_input(value: CustomerInput) -> None:
    if not isinstance(value, CustomerInput) or not isinstance(value.external_customer_ref, str):
        raise IntegrationError("invalid_request")
    if not REFERENCE_PATTERN.fullmatch(value.external_customer_ref):
        raise IntegrationError("invalid_request")
    name = value.display_name
    if not isinstance(name, str) or len(name) > 128 or any(unicodedata.category(char).startswith("C") for char in name):
        raise IntegrationError("invalid_request")
    if len(name.encode("utf-8")) > 512:
        raise IntegrationError("invalid_request")


def _customer_query(authority: IntegrationAuthority) -> Select[tuple[Customer, IntegrationCustomer, bool]]:
    linked = select(CustomerTelegramLink.id).where(
        CustomerTelegramLink.integration_id == authority.integration_id,
        CustomerTelegramLink.customer_id == Customer.id, CustomerTelegramLink.ended_at.is_(None),
    ).correlate(Customer).exists()
    return select(Customer, IntegrationCustomer, linked).join(
        IntegrationCustomer, IntegrationCustomer.customer_id == Customer.id,
    ).join(Integration, Integration.id == IntegrationCustomer.integration_id).where(
        Integration.id == authority.integration_id,
        Integration.owner_principal_id == authority.owner.principal_id,
        Integration.scope_kind == authority.shop.kind.value, Integration.shop_id == authority.shop.shop_id,
        Customer.legacy_bot_user_id.is_(None),
    ).execution_options(populate_existing=True)


def _view(customer: Customer, mapping: IntegrationCustomer, linked: bool = False) -> CustomerView:
    if customer.state != "active":
        raise IntegrationError("customer_blocked")
    return CustomerView(customer.public_id, mapping.external_customer_ref, mapping.display_name,
                        customer.state, linked, utc_time(customer.created_at))


async def read_integration_customer(session: AsyncSession, bearer: str, customer_id: str) -> CustomerView:
    authority = await resolve_integration_authority(session, bearer, required_scopes=frozenset({"customers:read"}))
    if not isinstance(customer_id, str) or not CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        raise IntegrationError("not_found")
    row = (await session.execute(_customer_query(authority).where(Customer.public_id == customer_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    return _view(*row)


async def _register(
    session: AsyncSession, bearer: str, value: CustomerInput,
) -> CustomerRegistration:
    # Credential/management mutations share this lock; authorization is reloaded after waiting.
    await verify_credential(session, bearer, lock_integration=True)
    authority = await resolve_integration_authority(session, bearer, required_scopes=frozenset({"customers:write"}))
    policy = await session.scalar(select(Integration.customer_entry_policy).where(Integration.id == authority.integration_id))
    if policy != "website":
        raise IntegrationError("website_entry_not_enabled")
    existing = (await session.execute(_customer_query(authority).where(
        IntegrationCustomer.external_customer_ref == value.external_customer_ref,
    ))).one_or_none()
    if existing is not None:
        customer = _view(*existing)
        if customer.display_name != value.display_name:
            raise IntegrationError("customer_reference_conflict")
        return CustomerRegistration(customer, False)
    customer = Customer()
    session.add(customer)
    await session.flush()
    mapping = IntegrationCustomer(integration_id=authority.integration_id, customer_id=customer.id,
                                  external_customer_ref=value.external_customer_ref, display_name=value.display_name)
    session.add(mapping)
    await session.flush()
    return CustomerRegistration(_view(customer, mapping), True)


async def create_or_get_integration_customer(
    session: AsyncSession, bearer: str, value: CustomerInput, *, commit: bool = True,
) -> CustomerRegistration:
    """Own the unit of work; failures/cancellation roll back the customer and mapping together.

    Future routes must first use the existing bearer/transport/write-budget dependency.
    This internal boundary reauthenticates; an authority object alone is never accepted.
    """
    validate_customer_input(value)
    try:
        result = await _register(session, bearer, value)
        await finish_integration_write(session, commit=commit)
        return result
    except BaseException:
        await session.rollback()
        raise
