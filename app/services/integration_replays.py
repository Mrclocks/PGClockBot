"""Short-lived HTTP request keys bind method, scope and canonical input across routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_read_models import IntegrationRequestReplay
from app.services.integration_authority import IntegrationAuthority
from app.services.integration_policy import IntegrationError
from app.services.integration_rate_limits import conflict_insert
from app.services.integration_request_keys import canonical_digest, request_key_digest

REPLAY_TTL = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RequestBinding:
    method: str
    route: str
    customer_id: str | None
    body: dict
    version: str = "v1"


async def lookup_replay(session: AsyncSession, authority: IntegrationAuthority, key: str, binding: RequestBinding) -> str | None:
    row = await session.scalar(select(IntegrationRequestReplay).where(
        IntegrationRequestReplay.integration_id == authority.integration_id,
        IntegrationRequestReplay.key_digest == request_key_digest(key),
        IntegrationRequestReplay.expires_at > datetime.now(timezone.utc),
    ).execution_options(populate_existing=True))
    if row is None:
        return None
    if row.input_digest != canonical_digest(asdict(binding)):
        raise IntegrationError("idempotency_conflict")
    return row.resource_id


async def bind_replay(session: AsyncSession, authority: IntegrationAuthority, key: str, binding: RequestBinding,
                      *, resource_kind: str, resource_id: str) -> None:
    existing = await lookup_replay(session, authority, key, binding)
    if existing is not None:
        if existing != resource_id:
            raise IntegrationError("idempotency_conflict")
        return
    now = datetime.now(timezone.utc)
    table = IntegrationRequestReplay.__table__
    statement = conflict_insert(session, table).values(
        integration_id=authority.integration_id, key_digest=request_key_digest(key), input_digest=canonical_digest(asdict(binding)),
        resource_kind=resource_kind, resource_id=resource_id, expires_at=now+REPLAY_TTL,
    )
    await session.execute(statement.on_conflict_do_update(
        index_elements=["integration_id", "key_digest"],
        set_={"input_digest": statement.excluded.input_digest, "resource_kind": resource_kind,
              "resource_id": resource_id, "expires_at": now+REPLAY_TTL},
        where=table.c.expires_at <= now,
    ))
    if await lookup_replay(session, authority, key, binding) != resource_id:
        raise IntegrationError("idempotency_conflict")
