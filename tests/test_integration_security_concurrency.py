"""Independent connection races on SQLite and disposable PostgreSQL schemas."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.integration_security_models import IntegrationRateCounter as Counter, IntegrationSecurityAudit as Audit
from app.services.integration_rate_limits import (
    LIMITS, IntegrationRateLimited, RateWindow, RequestBudget, charge_integration, reserve_preauth,
)
from app.services.integration_security_audit import record_security_outcome


async def race(url: str, schema: str | None) -> None:
    options = {"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}
    engine = create_async_engine(url, **options)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    window = RateWindow(1000, 60)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Counter(subject="intg_synthetic", budget="read", window=1000, used=119))
            session.add(Counter(subject="ip_synthetic", budget="preauth", window=1000, used=59))
            await session.commit()

        async def read() -> bool:
            async with sessions() as session:
                try:
                    await charge_integration(session, "intg_synthetic", RequestBudget.READ, charged=set())
                    return True
                except IntegrationRateLimited:
                    return False

        async def authenticate() -> bool:
            async with sessions() as session:
                try:
                    await reserve_preauth(session, "ip_synthetic", window)
                    return True
                except IntegrationRateLimited:
                    return False

        async def audit() -> None:
            async with sessions() as session:
                await record_security_outcome(session, f"req_{uuid4()}", "invalid_credential")
                await session.commit()

        with patch("app.services.integration_rate_limits.rate_window", return_value=window):
            assert sum(await asyncio.gather(*(read() for _ in range(8)))) == 1
        assert sum(await asyncio.gather(*(authenticate() for _ in range(8)))) == 1
        with patch.dict(LIMITS, audit=2), patch("app.services.integration_security_audit.rate_window", return_value=window):
            await asyncio.gather(*(audit() for _ in range(10)))
        async with sessions() as session:
            assert await session.scalar(sa.select(Counter.used).where(Counter.subject == "intg_synthetic")) == 120
            assert await session.scalar(sa.select(Counter.used).where(Counter.subject == "ip_synthetic")) == 60
            assert await session.scalar(sa.select(sa.func.count()).select_from(Audit)) == 3
            assert await session.scalar(sa.select(sa.func.sum(Audit.occurrences))) == 10
    finally:
        await engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_workers_cannot_exceed_limits_or_audit_detail_cap(dialect: str, tmp_path: Path) -> None:
    if dialect == "sqlite":
        asyncio.run(race(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}", None))
        return
    url = os.environ.get("PGCLOCK_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("PGCLOCK_TEST_POSTGRES_URL must select an isolated synthetic test database")
    schema = f"integration_security_race_{uuid4().hex}"
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
    try:
        asyncio.run(race(url.replace("postgresql+psycopg:", "postgresql+asyncpg:"), schema))
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()
