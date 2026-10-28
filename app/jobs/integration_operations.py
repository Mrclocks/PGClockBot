"""Default-off scheduling adapters for durable execution and signed notifications."""

from __future__ import annotations

from app.db.session import SessionLocal
from app.services.integration_runtime import integration_capacity_retained, integration_execution_active
from app.services.integration_workers import run_operation_batch


async def run_integration_operations() -> None:
    execute = integration_execution_active()
    if not execute and not integration_capacity_retained():
        return
    await run_operation_batch(SessionLocal, execute=execute)


