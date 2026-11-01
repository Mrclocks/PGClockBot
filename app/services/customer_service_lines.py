"""The customer's services as short lines for a staff card, from stored data only (no panel call)."""

from __future__ import annotations

import html
from collections.abc import Sequence

from app.db.models import UserService
from app.services.formatting import format_bytes, format_expire_short

SERVICE_LINE_LIMIT = 5
STATUS_LABELS = {
    "active": "فعال",
    "disabled": "غیرفعال",
    "expired": "منقضی",
    "limited": "حجم تمام‌شده",
    "on_hold": "در انتظار اتصال",
}
_ACTIVE_STATUSES = frozenset({"active", "on_hold"})


def _status_label(service: UserService) -> str:
    if service.is_cancelled:
        return "لغو شده"
    return STATUS_LABELS.get(service.quota_status or "", "نامشخص")


def _is_active(service: UserService) -> bool:
    return not service.is_cancelled and (service.quota_status or "") in _ACTIVE_STATUSES


def _line(service: UserService) -> str:
    plan = getattr(service, "plan", None)
    parts = [f"<code>{html.escape(service.pg_username)}</code>"]
    if plan is not None and plan.name:
        parts.append(html.escape(plan.name))
    parts.append(_status_label(service))
    if service.quota_synced_at is not None:
        limit = int(service.quota_data_limit_bytes or 0)
        parts.append(f"انقضا {format_expire_short(service.quota_expire_at)}")
        parts.append(f"حجم {format_bytes(limit) if limit > 0 else 'نامحدود'}")
    return "• " + " · ".join(parts)


def service_lines(services: Sequence[UserService], *, limit: int = SERVICE_LINE_LIMIT) -> list[str]:
    """Active services first (newest first within each group), at most ``limit`` lines plus a count of the rest."""
    ordered = sorted(services, key=lambda s: (not _is_active(s), -int(s.id)))
    lines = [_line(service) for service in ordered[:limit]]
    if len(ordered) > limit:
        lines.append(f"و {len(ordered) - limit} سرویس دیگر — جزئیات در وب‌پنل")
    return lines
