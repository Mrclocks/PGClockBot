"""Run saved connection monitoring independently of Telegram availability."""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.connection_monitor_overview import monitor_runtime_enabled
from app.services.connection_monitor_worker import run_monitor_cycle
from app.services.xray_probe_resources import XrayBinary


logger = logging.getLogger(__name__)


async def run_connection_status() -> None:
    async with SessionLocal() as session:
        if not await monitor_runtime_enabled(session):
            return
    settings = get_settings()
    binary = XrayBinary(Path(settings.connection_monitor_xray_path), settings.connection_monitor_xray_sha256)
    try:
        await run_monitor_cycle(SessionLocal, binary)
    except Exception:
        # Avoid exception payloads: third-party errors can contain subscription URLs.
        logger.warning("Connection monitoring cycle failed; expired leases remain reclaimable")
