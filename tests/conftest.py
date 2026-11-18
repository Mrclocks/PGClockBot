"""Test-run wiring for PostgreSQL: the app URL and per-test database cleanup."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# The app builds its engine from DATABASE_URL at import time. Point it at the
# test server before any app module loads, so no test can reach a database
# named in a developer's .env.
if os.environ.get("TEST_DATABASE_URL", "").strip():
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"].strip()

from tests import postgres_support  # noqa: E402  (must follow the DATABASE_URL override)


@pytest.fixture(autouse=True)
def _postgres_database_scope() -> Iterator[None]:
    """Drop the databases a test created through the scoped_* helpers when it ends."""
    scope = postgres_support.begin_scope()
    try:
        yield
    finally:
        postgres_support.end_scope(scope)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    postgres_support.end_session()
