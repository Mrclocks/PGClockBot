"""Shared rate limits and authentication evidence tested without external services."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.integration_auth import PGVerifier, integration_bearer_dependency, register_integration_auth_errors
from app.api.integration_transport import IntegrationPeerCapture
from app.db import Base
from app.db.integration_security_models import IntegrationRateCounter as Counter, IntegrationSecurityAudit as Audit
from app.db.models import ResellerProfile
from app.services.integration_authority import IntegrationAuthority, IntegrationPGEvidence
from app.services.integration_credentials import CredentialIssuance, issue_credential, revoke_credential, rotate_credential
from app.services.integration_policy import IntegrationError
from app.services.integration_rate_limits import (
    LIMITS, IntegrationRateLimited, RateWindow, RequestBudget, charge_integration, rate_window,
    release_preauth, reserve_preauth,
)
from app.services.integration_security_audit import record_security_outcome
from app.services.integration_security_retention import prune_security_records
from app.services.integrations import configure_integration
from test_integration_concurrency import STAFF, seed

SECRET = "synthetic-integration-security-key-0001"
SETTINGS = SimpleNamespace(web_secret=SECRET, integration_trusted_proxies="")
SCOPES = frozenset({"customers:read", "services:read", "services:access"})
NOW = datetime(2026, 10, 21, 12, 0, 20, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_rate_clock() -> Iterator[None]:
    with patch("app.services.integration_rate_limits.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        yield


@asynccontextmanager
async def database(path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str, CredentialIssuance]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            integration_id = await seed(session)
            await session.execute(update(ResellerProfile).values(web_permissions=
                "integrations_manage,integration_customers_read,integration_services_read,integration_service_access"))
            await session.commit()
            await configure_integration(session, STAFF, integration_id, status="active", grants=SCOPES)
            key = await issue_credential(session, STAFF, integration_id, scopes=SCOPES)
        yield sessions, integration_id, key
    finally:
        await engine.dispose()


def application(sessions: async_sessionmaker, *, budget: RequestBudget = RequestBudget.READ,
                verifier: PGVerifier | None = None, capture: bool = True, scopes: frozenset[str] = frozenset()) -> FastAPI:
    app = FastAPI()
    if capture:
        app.add_middleware(IntegrationPeerCapture)
    register_integration_auth_errors(app)

    async def get_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    dependency = integration_bearer_dependency(get_db, budget=budget, pg_verifier=verifier, required_scopes=scopes)

    @app.get("/protected")
    async def protected(authority: IntegrationAuthority = Depends(dependency)) -> dict:
        return {"integration_id": authority.integration_id}

    return app


async def request(app: FastAPI, bearer: str | None = None, *, peer: tuple[str, int] | None = ("198.51.100.9", 1234), headers: dict[str, str] | None = None) -> httpx.Response:
    values = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    values.update(headers or {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer), base_url="https://test") as client:
        return await client.get("/protected", headers=values)


@pytest.mark.parametrize("budget,limit", [(RequestBudget.READ, 120), (RequestBudget.WRITE, 20), (RequestBudget.PRIVATE, 10)])
def test_exact_boundaries_and_utc_window_rollover(tmp_path: Path, budget: RequestBudget, limit: int) -> None:
    async def run() -> None:
        async with database(tmp_path / "limits.db") as (sessions, integration_id, _):
            async with sessions() as session:
                with patch("app.services.integration_rate_limits.rate_window", return_value=rate_window(NOW)):
                    for _ in range(limit):
                        await charge_integration(session, integration_id, budget, charged=set())
                    with pytest.raises(IntegrationRateLimited) as caught:
                        await charge_integration(session, integration_id, budget, charged=set())
                    assert caught.value.retry_after == 40
                with patch("app.services.integration_rate_limits.rate_window", return_value=rate_window(NOW + timedelta(minutes=1))):
                    await charge_integration(session, integration_id, budget, charged=set())
                if budget == RequestBudget.PRIVATE:
                    counts = dict((await session.execute(select(Counter.budget, Counter.used).where(
                        Counter.subject == integration_id, Counter.window == rate_window(NOW).minute))).all())
                    assert counts == {"read": 10, "private": 10}
    asyncio.run(run())


def test_shared_credentials_rotation_rechecks_and_budget_configuration(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "credentials.db") as (sessions, integration_id, key):
            async with sessions() as session:
                second = await issue_credential(session, STAFF, integration_id, scopes=SCOPES)
            async def verifier(authority: IntegrationAuthority) -> IntegrationPGEvidence:
                async with sessions() as session:
                    assert not session.in_transaction()
                return IntegrationPGEvidence(11, "pg-race", datetime.now(timezone.utc), True, frozenset({"read"}))
            app = application(sessions, budget=RequestBudget.PRIVATE, scopes=frozenset({"services:access"}), verifier=verifier)
            with patch("app.api.integration_auth.get_settings", return_value=SETTINGS):
                for number in range(10):
                    response = await request(app, key.bearer if number % 2 else second.bearer,
                                             headers={"X-Request-ID": "attacker", "X-Rate-Budget": "write"})
                    assert response.status_code == 200
                    assert response.headers["X-Request-ID"].startswith("req_")
                    assert response.headers["X-Request-ID"] != "attacker"
                async with sessions() as session:
                    replacement = await rotate_credential(session, STAFF, integration_id, second.credential.selector)
                response = await request(app, replacement.bearer)
                assert response.status_code == 429
                assert 1 <= int(response.headers["Retry-After"]) <= 60
                assert response.json()["error"]["request_id"] != "attacker"
            async with sessions() as session:
                counts = dict((await session.execute(select(Counter.budget, Counter.used).where(Counter.subject == integration_id))).all())
                assert counts == {"read": 10, "private": 10}
                assert await session.scalar(select(Counter.used).where(Counter.budget == "preauth")) == 0
    asyncio.run(run())


def test_failure_budget_admission_releases_successes_and_bounds_pending_attempts(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "preauth.db") as (sessions, _, _):
            window = rate_window(NOW)
            async def reserve() -> bool:
                async with sessions() as session:
                    try:
                        await reserve_preauth(session, "synthetic-ip", window)
                        return True
                    except IntegrationRateLimited:
                        return False
            results = await asyncio.gather(*(reserve() for _ in range(65)))
            assert sum(results) == 60
            async with sessions() as session:
                await release_preauth(session, "synthetic-ip", window)
                await session.commit()
            assert await reserve()
            assert not await reserve()
            assert await reserve_next(sessions, window)
            async with sessions() as session:
                # Sequential successes are not an all-traffic limit.
                for _ in range(65):
                    await reserve_preauth(session, "successful-ip", window)
                    await release_preauth(session, "successful-ip", window)
                    await session.commit()
    asyncio.run(run())


async def reserve_next(sessions: async_sessionmaker, window: RateWindow) -> bool:
    async with sessions() as session:
        await reserve_preauth(session, "synthetic-ip", replace(window, minute=window.minute + 1))
    return True


def test_http_failure_limit_untrusted_selector_secret_exclusion_and_tenant_isolation(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "failures.db") as (sessions, integration_id, key):
            app = application(sessions)
            forged = key.bearer[:-1] + ("0" if key.bearer[-1] != "0" else "1")
            with patch("app.api.integration_auth.get_settings", return_value=SETTINGS):
                for _ in range(60):
                    assert (await request(app, forged)).status_code == 401
                blocked = await request(app, key.bearer)
                assert blocked.status_code == 429
                assert (await request(app, key.bearer, peer=("198.51.100.10", 1234))).status_code == 200
            async with sessions() as session:
                rows = list(await session.scalars(select(Audit)))
                anonymous = [row for row in rows if row.outcome != "succeeded"]
                assert len(anonymous) == 61
                assert all(row.integration_id is None and row.shop_id is None for row in anonymous)
                scoped = [row for row in rows if row.integration_id == integration_id]
                assert len(scoped) == 1 and scoped[0].shop_id == 11
                assert await session.scalar(select(func.count()).select_from(Audit).where(Audit.shop_id == 12)) == 0
                values = repr((await session.execute(Audit.__table__.select())).all())
                digest = hashlib.sha256(key.bearer.split(".")[1].encode()).hexdigest()
                for secret in (key.bearer, forged, digest, key.bearer.split(".")[1], "198.51.100.9", "Authorization"):
                    assert secret not in values
                assert len(scoped[0].ip_digest) == 64
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["counter", "audit", "commit"])
def test_storage_failures_fail_closed_with_safe_error(tmp_path: Path, failure: str) -> None:
    async def run() -> None:
        async with database(tmp_path / "failure.db") as (sessions, _, key):
            targets = {"counter": "app.services.integration_rate_limits.take_slot",
                       "audit": "app.services.integration_security.record_security_outcome",
                       "commit": "app.services.integration_security.AuthenticationAttempt.finish"}
            error = OperationalError("sensitive bearer=" + key.bearer, {}, Exception("secret"))
            with patch("app.api.integration_auth.get_settings", return_value=SETTINGS), patch(
                targets[failure], new=AsyncMock(side_effect=error),
            ):
                response = await request(application(sessions), key.bearer)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "security_unavailable"
            assert key.bearer not in response.text
    asyncio.run(run())


def test_verifier_has_no_open_transaction_and_revocation_is_rechecked(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "recheck.db") as (sessions, integration_id, key):
            async def verifier(authority: IntegrationAuthority) -> IntegrationPGEvidence:
                async with sessions() as session:
                    await revoke_credential(session, STAFF, integration_id, key.credential.selector)
                return IntegrationPGEvidence(11, "pg-race", datetime.now(timezone.utc), True, frozenset({"read"}))
            with patch("app.api.integration_auth.get_settings", return_value=SETTINGS):
                result = await request(application(sessions, verifier=verifier, scopes=frozenset({"services:read"})), key.bearer)
            assert result.status_code == 401
            async with sessions() as session:
                assert await session.scalar(select(Counter.used).where(Counter.subject == integration_id)) == 1
                row = await session.scalar(select(Audit))
                assert row.integration_id == integration_id and row.outcome == "invalid_credential"
    asyncio.run(run())


def test_missing_transport_or_unconfigured_secret_fails_closed(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "transport.db") as (sessions, _, key):
            with patch("app.api.integration_auth.get_settings", return_value=SETTINGS):
                assert (await request(application(sessions, capture=False), key.bearer)).status_code == 503
                assert (await request(application(sessions), key.bearer, peer=None)).status_code == 503
            with patch("app.api.integration_auth.get_settings", return_value=SimpleNamespace(
                web_secret="change-me", integration_trusted_proxies="",
            )):
                assert (await request(application(sessions), key.bearer)).status_code == 503
    asyncio.run(run())


def test_audit_flood_has_global_detail_cap_and_fixed_anonymous_overflow(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "flood.db") as (sessions, _, _):
            with patch.dict(LIMITS, audit=2):
                async with sessions() as session:
                    for _ in range(10):
                        await record_security_outcome(session, "req_synthetic", "invalid_credential", ip_digest="a"*64)
                        await session.commit()
                    rows = list(await session.scalars(select(Audit)))
                    assert len(rows) == 3
                    overflow = next(row for row in rows if row.id.startswith("overflow:"))
                    assert overflow.occurrences == 8 and overflow.integration_id is None and overflow.ip_digest is None
                    assert sum(row.occurrences for row in rows) == 10
                    with pytest.raises(IntegrationError):
                        await record_security_outcome(session, "req_synthetic", "arbitrary secret text")
    asyncio.run(run())


def test_retention_is_bounded_preserves_held_evidence_and_current_records(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "retention.db") as (sessions, _, _):
            now = datetime.now(timezone.utc)
            async with sessions() as session:
                session.add_all([Counter(subject=str(number), budget="read", window=rate_window(now).minute-2881, used=1)
                                 for number in range(503)])
                session.add(Counter(subject="recent", budget="preauth", window=rate_window(now).minute, used=60))
                session.add_all([Audit(id=str(number), request_id="req_synthetic", outcome="invalid_credential",
                    created_at=now-timedelta(days=181), evidence_hold=number == 0) for number in range(503)])
                session.add(Audit(id="recent", request_id="req_synthetic", outcome="succeeded", created_at=now))
                await session.commit()
                assert await prune_security_records(session, now=now) == (500, 500)
                assert await prune_security_records(session, now=now) == (3, 2)
                assert {row.id for row in await session.scalars(select(Audit))} == {"0", "recent"}
                assert await session.scalar(select(Counter.used)) == 60
    asyncio.run(run())


def test_private_scope_cannot_be_configured_without_private_budget() -> None:
    with pytest.raises(ValueError):
        integration_bearer_dependency(lambda: None, required_scopes=frozenset({"services:access"}))
    with pytest.raises(ValueError):
        integration_bearer_dependency(lambda: None, budget="read")


def test_write_scopes_require_explicit_write_configuration() -> None:
    with pytest.raises(ValueError):
        integration_bearer_dependency(lambda: None, required_scopes=frozenset({"customers:write"}))
    integration_bearer_dependency(lambda: None, required_scopes=frozenset({"customers:write"}), budget=RequestBudget.WRITE)


def test_trusted_forwarding_and_raw_tls_are_used_at_http_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "http-proxy.db") as (sessions, _, key):
            app = application(sessions)
            with patch("app.api.integration_auth.get_settings", return_value=SimpleNamespace(
                web_secret=SECRET, integration_trusted_proxies="10.0.0.1/32",
            )):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.0.0.1", 123)),
                                             base_url="http://test") as client:
                    response = await client.get("/protected", headers={"Authorization": f"Bearer {key.bearer}",
                        "X-Forwarded-For": "198.51.100.3", "X-Forwarded-Proto": "https"})
                    assert response.status_code == 200
                    response = await client.get("/protected", headers={"Authorization": f"Bearer {key.bearer}",
                        "X-Forwarded-For": "malformed", "X-Forwarded-Proto": "https"})
                    assert response.status_code == 503
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)),
                                             base_url="http://test") as client:
                    response = await client.get("/protected", headers={"Authorization": f"Bearer {key.bearer}",
                        "X-Forwarded-For": "198.51.100.3", "X-Forwarded-Proto": "https"})
                    assert response.status_code == 400
    asyncio.run(run())


def test_audit_failure_does_not_undo_committed_rate_charge(tmp_path: Path) -> None:
    async def run() -> None:
        async with database(tmp_path / "audit-failure.db") as (sessions, integration_id, key):
            error = OperationalError("synthetic storage failure", {}, Exception("sensitive"))
            with patch("app.api.integration_auth.get_settings", return_value=SETTINGS), patch(
                "app.services.integration_security.record_security_outcome", new=AsyncMock(side_effect=error),
            ):
                result = await request(application(sessions), key.bearer)
                assert result.status_code == 503
            async with sessions() as session:
                assert await session.scalar(select(Counter.used).where(Counter.subject == integration_id)) == 1
                # Failure rolls back the successful-attempt release; no silent admission results.
                assert await session.scalar(select(Counter.used).where(Counter.budget == "preauth")) == 1
                assert await session.scalar(select(func.count()).select_from(Audit)) == 0
    asyncio.run(run())
