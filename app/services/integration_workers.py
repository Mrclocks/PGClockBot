"""Bounded orchestration over durable work; each item has an independent failure boundary."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.integration_operation_models import IntegrationOperation
from app.services.integration_execution import execute_integration_operation
from app.services.integration_operation_claims import QUEUE_TTL, expire_queued_operation, recover_expired_operation

logger = logging.getLogger(__name__)


async def run_operation_batch(sessions: async_sessionmaker[AsyncSession], *, execute: bool) -> None:
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        expired = list(await session.scalars(select(IntegrationOperation.id).where(or_(
            (IntegrationOperation.state == "running") & (IntegrationOperation.lease_expires_at <= now),
            (IntegrationOperation.state == "queued") & (IntegrationOperation.created_at <= now-QUEUE_TTL),
        )).order_by(IntegrationOperation.id).limit(100)))
    for operation_id in expired:
        try:
            async with sessions() as session:
                await recover_expired_operation(session, operation_id, now=now)
                await expire_queued_operation(session, operation_id, now=now)
        except Exception as error:
            logger.warning("Integration recovery unavailable operation=%s type=%s", operation_id, type(error).__name__)
    if not execute:
        return
    async with sessions() as session:
        ranked = select(IntegrationOperation.id, func.row_number().over(partition_by=IntegrationOperation.integration_id,
            order_by=IntegrationOperation.id).label("position")).where(IntegrationOperation.state == "queued").subquery()
        queued = list(await session.scalars(select(ranked.c.id).where(ranked.c.position <= 2).order_by(ranked.c.id).limit(8)))
    await asyncio.gather(*(_execute_item(sessions, operation_id) for operation_id in queued))


async def _execute_item(sessions: async_sessionmaker[AsyncSession], operation_id: int) -> None:
    try:
        await execute_integration_operation(sessions, operation_id)
    except Exception as error:
        logger.warning("Integration item needs recovery operation=%s type=%s", operation_id, type(error).__name__)




