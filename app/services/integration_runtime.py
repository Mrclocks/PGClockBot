"""Process activation snapshot for optional integration work in legacy hot paths."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Setting
from app.services.integration_rate_limits import conflict_insert

_api_enabled = False
_has_execution_history = False
EXECUTION_HISTORY_KEY = "integration_execution_started"


def observe_api_switch(value: str | None) -> None:
    """Update the single global switch after a committed write or authoritative read."""
    global _api_enabled
    _api_enabled = value == "1"


def observe_committed_integration_settings(values: Mapping[str, str]) -> None:
    value = values.get("integration_api_enabled")
    if value is not None:
        observe_api_switch(value)


def integration_execution_active() -> bool:
    return get_settings().integration_execution_enabled and _api_enabled


def observe_execution_history(keys: set[str]) -> None:
    """Execution history is monotonic until a database restore and process restart."""
    global _has_execution_history
    _has_execution_history = _has_execution_history or EXECUTION_HISTORY_KEY in keys


def integration_capacity_retained() -> bool:
    return integration_execution_active() or _has_execution_history


async def retain_execution_history(session: AsyncSession) -> None:
    """Persist with the saved target so disabling execution never frees uncertain capacity."""
    await session.execute(conflict_insert(session, Setting.__table__).values(
        key=EXECUTION_HISTORY_KEY, value="1",
    ).on_conflict_do_nothing(index_elements=["key"]))
    observe_execution_history({EXECUTION_HISTORY_KEY})


async def load_integration_runtime(session: AsyncSession) -> None:
    """Warm once before clients/workers start; out-of-process changes require restart."""
    if not get_settings().integration_execution_enabled:
        observe_api_switch(None)
        return
    value = await session.scalar(select(Setting.value).where(Setting.key == "integration_api_enabled"))
    observe_api_switch(value)
