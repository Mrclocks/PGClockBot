"""Owner-facing monitoring snapshot and fixed error texts shared by the bot and the web panel."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ConnectionMonitorState
from app.services.connection_monitor_access import MONITOR_ACCESS_DENIED, MonitorAccess
from app.services.connection_monitor_actions import INVALID_PREFERENCES
from app.services.connection_monitor_preferences import PREFERENCES_UNAVAILABLE
from app.services.connection_monitor_queue import QUEUE_UNAVAILABLE
from app.services.connection_monitor_settings import ACCOUNT_UNAVAILABLE, LOCAL_PROBE_ID
from app.services.connection_monitor_snapshots import stored_utc
from app.services.connection_monitor_views import MonitorTargetView, monitor_admin_page
from app.services.connection_status_policy import is_fresh
from app.services.connection_status_texts import REASON_LABELS
from app.services.connection_status_types import ConnectionStatus
from app.services.xray_probe_resources import XrayBinary, XrayProbeError


logger = logging.getLogger(__name__)

GENERIC_ERROR = "پایش اکنون در دسترس نیست؛ کمی بعد دوباره تلاش کنید."
CORE_NOT_CONFIGURED = "not_configured"
CORE_CONFIGURED = "configured"
CORE_READY = "ready"
CORE_INVALID = "invalid"

# Domain messages are fixed Persian strings; panel, database and core errors never reach a user.
SAFE_ERRORS = frozenset({
    MONITOR_ACCESS_DENIED, INVALID_PREFERENCES, PREFERENCES_UNAVAILABLE, QUEUE_UNAVAILABLE, ACCOUNT_UNAVAILABLE,
    "هاست پایش یافت نشد.", "صفحه نامعتبر است.", "درخواست نامعتبر است.", "شناسه نامعتبر است.",
})
STATUS_LABELS = {
    ConnectionStatus.HEALTHY: "در دسترس", ConnectionStatus.DEGRADED: "اختلال", ConnectionStatus.DOWN: "خارج از دسترس",
    ConnectionStatus.UNKNOWN: "نامشخص", ConnectionStatus.NOT_READY: "آمادهٔ تست نیست", ConnectionStatus.PAUSED: "متوقف",
}
STATUS_TONES = {
    ConnectionStatus.HEALTHY: "ok", ConnectionStatus.DEGRADED: "warn", ConnectionStatus.DOWN: "danger",
}


def safe_monitor_error(error: Exception) -> str:
    """Return the fixed message for a domain rule violation, otherwise a generic one."""
    if type(error) is ValueError and str(error) in SAFE_ERRORS:
        return str(error)
    return GENERIC_ERROR


async def monitor_runtime_enabled(session: AsyncSession) -> bool:
    """Cheap read so a disabled monitor costs the scheduler one SELECT and no lease write."""
    enabled = await session.scalar(select(ConnectionMonitorState.enabled).where(
        ConnectionMonitorState.reseller_id.is_(None), ConnectionMonitorState.probe_id == LOCAL_PROBE_ID,
    ).limit(1))
    return enabled is True


async def core_state(*, verify: bool) -> str:
    """Report the deployment's Xray core without ever returning its path or checksum."""
    deployment = get_settings()
    if not deployment.connection_monitor_xray_path or not deployment.connection_monitor_xray_sha256:
        return CORE_NOT_CONFIGURED
    if not verify:
        return CORE_CONFIGURED
    binary = XrayBinary(Path(deployment.connection_monitor_xray_path), deployment.connection_monitor_xray_sha256)
    try:
        await asyncio.to_thread(binary.verify)
    except XrayProbeError:
        return CORE_INVALID
    return CORE_READY


def target_payload(view: MonitorTargetView) -> dict[str, Any]:
    health = view.health
    return {
        "id": view.id, "host_id": view.host_id, "public_name": view.public_name, "display_order": view.display_order,
        "enabled": view.test_enabled, "is_public": view.is_public, "manual_pending": view.manual_pending,
        "status": health.status.value, "status_label": STATUS_LABELS[health.status],
        "tone": STATUS_TONES.get(health.status, "neutral"), "delay_ms": health.delay_ms,
        "covered": health.covered, "expected": health.expected,
        "checked_at": health.checked_at.isoformat() if health.checked_at else None,
        "reason": view.reason, "reason_label": REASON_LABELS.get(view.reason or "", ""),
    }


async def monitor_overview(session: AsyncSession, access: MonitorAccess, *, now: datetime,
                           after_id: int = 0, verify_core: bool = False) -> dict[str, Any]:
    page = await monitor_admin_page(session, access, now=now, after_id=after_id)
    state = await session.scalar(select(ConnectionMonitorState).where(
        ConnectionMonitorState.id == page.settings.id, ConnectionMonitorState.reseller_id.is_(None),
        ConnectionMonitorState.probe_id == LOCAL_PROBE_ID,
    ).limit(1))
    synced_at = stored_utc(state.catalog_synced_at) if state is not None else None
    result = {
        "ok": True, "enabled": page.settings.enabled, "has_account": page.settings.has_account,
        "account_username": state.pg_username if state is not None and page.settings.has_account else None,
        "core": await core_state(verify=verify_core),
        "catalog_synced_at": synced_at.isoformat() if synced_at else None,
        "catalog_fresh": is_fresh(synced_at, now=now),
        "targets": [target_payload(view) for view in page.targets], "next_id": page.next_id,
    }
    await session.commit()
    return result
