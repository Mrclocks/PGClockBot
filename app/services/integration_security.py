"""Coordinate admission and durable authentication outcomes without network transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.integration_client_ip import IntegrationClient, ip_identifier
from app.services.integration_rate_limits import RateWindow, rate_window, release_preauth, reserve_preauth
from app.services.integration_security_audit import VerifiedIntegration, record_security_outcome

AUTH_FAILURES = frozenset({
    "invalid_request", "unsupported_authentication", "invalid_credential", "integration_disabled",
    "principal_inactive", "permission_denied",
})


@dataclass(slots=True)
class AuthenticationAttempt:
    request_id: str = field(default_factory=lambda: f"req_{uuid4()}")
    window: RateWindow = field(default_factory=rate_window)
    ip_digest: str | None = None
    identity: VerifiedIntegration | None = None
    charged: set[str] = field(default_factory=set)
    reserved: bool = False
    settled: bool = False

    async def admit(self, session: AsyncSession, client: IntegrationClient, secret: str) -> None:
        if self.ip_digest is None:
            self.ip_digest = ip_identifier(client.address, secret)
        if not self.reserved and not self.settled:
            await reserve_preauth(session, self.ip_digest, self.window)
            self.reserved = True

    async def finish(self, session: AsyncSession, outcome: str) -> None:
        if self.reserved and not self.settled and outcome not in AUTH_FAILURES:
            assert self.ip_digest is not None
            await release_preauth(session, self.ip_digest, self.window)
        await record_security_outcome(
            session, self.request_id, outcome, identity=self.identity, ip_digest=self.ip_digest,
        )
        await session.commit()
        self.settled = True
