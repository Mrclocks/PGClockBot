"""Test-run wiring for PostgreSQL: the app URL and per-test database cleanup."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import pytest

# The app builds its engine from DATABASE_URL at import time. Point it at the
# test server before any app module loads, so no test can reach a database
# named in a developer's .env.
if os.environ.get("TEST_DATABASE_URL", "").strip():
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"].strip()

try:
    from tests import postgres_support  # must follow the DATABASE_URL override
except ModuleNotFoundError:
    # Lightweight CI jobs (the design-rule checks) install only pytest and run
    # no database test; everything else installs requirements-dev.txt.
    postgres_support = None
else:
    postgres_support.freeze_template_name()


@pytest.fixture(autouse=True)
def _postgres_database_scope() -> Iterator[None]:
    """Drop the databases a test created through the scoped_* helpers when it ends."""
    if postgres_support is None:
        yield
        return
    scope = postgres_support.begin_scope()
    try:
        yield
    finally:
        _forget_app_engine_connections()
        postgres_support.end_scope(scope)


def _forget_app_engine_connections() -> None:
    """Drop the app engine's pooled connections; each test runs its own event loop.

    asyncpg connections belong to the loop that opened them, so a connection a
    test left in the pool would fail in the next test. close=False drops them
    without touching their (already closed) loop.
    """
    session_module = sys.modules.get("app.db.session")
    if session_module is not None:
        session_module.engine.sync_engine.dispose(close=False)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if postgres_support is not None:
        postgres_support.end_session()
