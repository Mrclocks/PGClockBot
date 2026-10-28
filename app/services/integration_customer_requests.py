"""Customer HTTP registration and its key binding commit as one unit of work."""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.integration_authority import resolve_integration_authority
from app.services.integration_credentials import verify_credential
from app.services.integration_customers import CustomerInput, CustomerRegistration, create_or_get_integration_customer
from app.services.integration_policy import IntegrationError
from app.services.integration_replays import RequestBinding, bind_replay, lookup_replay
from app.services.integration_request_keys import request_key_digest


async def register_customer_request(session: AsyncSession, bearer: str, value: CustomerInput,
                                     *, idempotency_key: str) -> CustomerRegistration:
    request_key_digest(idempotency_key)
    try:
        await verify_credential(session, bearer, lock_integration=True)
        authority = await resolve_integration_authority(session, bearer, required_scopes=frozenset({"customers:write"}))
        binding = RequestBinding("POST", "/customers", None, asdict(value))
        existing = await lookup_replay(session, authority, idempotency_key, binding)
        result = await create_or_get_integration_customer(session, bearer, value, commit=False)
        if existing is not None and existing != result.customer.id:
            raise IntegrationError("idempotency_conflict")
        await bind_replay(session, authority, idempotency_key, binding, resource_kind="customer", resource_id=result.customer.id)
        await session.commit()
        return result
    except BaseException:
        await session.rollback()
        raise
