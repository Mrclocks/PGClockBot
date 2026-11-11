"""How the link to PasarGuard behaves: failures by kind and response times.

Kept in memory for the running process (one panel, one origin). It answers
"is it slow, and why?" after a change: timeouts, refused connections, DNS,
dropped connections, reverse-proxy errors or PasarGuard's own 5xx. Paths
and messages are never stored; subscription paths carry customer tokens.

A debounced DB row (`pg_link_health_state`) keeps a short summary across
restarts so the owner card is not blank after boot.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

WINDOW_SEC = 3600.0
PERSIST_WINDOW_SEC = 86400.0
PERSIST_INTERVAL_SEC = 60.0
PLATFORM_ROW_ID = 1
# Admins read clock times in Tehran time.
DISPLAY_ZONE = ZoneInfo("Asia/Tehran")
MAX_EVENTS = 2000
MAX_LATENCIES = 500

KIND_LABELS = {
    "timeout": "پاسخ نداد (timeout)",
    "connect": "اتصال برقرار نشد",
    "dns": "خطای DNS",
    "dropped": "اتصال وسط کار قطع شد",
    "gateway": "خطای پراکسی (502/503/504)",
    "server_error": "خطای خود پاسارگارد (5xx)",
    "auth": "ورود به پاسارگارد رد شد (نام کاربری/رمز یا API key)",
    "client_error": "پاسخ نامعتبر از پاسارگارد (4xx)",
}
BUDGET_EXHAUSTION_LABEL = "بودجه‌ی زمانی صفحه تمام شد"
PRIOR_BOOT_NOTE = "از قبل از آخرین راه‌اندازی"
_DNS_HINTS = ("name or service not known", "temporary failure in name resolution", "getaddrinfo", "nodename nor servname")

logger = logging.getLogger(__name__)
_last_persist_mono = 0.0


@dataclass(slots=True)
class _LinkStats:
    started_at: float = field(default_factory=time.time)
    events: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    latencies: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=MAX_LATENCIES))
    totals: Counter[str] = field(default_factory=Counter)
    # Page-budget timeouts are not panel outages; keep a separate bounded counter.
    budget_events: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    budget_total: int = 0
    reads: int = 0
    last_ok_at: float | None = None
    last_failure_at: float | None = None
    last_failure_kind: str | None = None


_stats = _LinkStats()


def reset() -> None:
    global _stats, _last_persist_mono
    _stats = _LinkStats()
    _last_persist_mono = 0.0


def reset_persist_debounce() -> None:
    """Tests only: allow the next persist without waiting the debounce."""
    global _last_persist_mono
    _last_persist_mono = 0.0


def failure_kind(exc: BaseException | None = None, status_code: int | None = None) -> str:
    """Name the cause of a failed read."""
    if status_code is not None:
        if status_code in (502, 503, 504):
            return "gateway"
        if status_code in (401, 403):
            return "auth"
        if 400 <= status_code < 500:
            return "client_error"
        return "server_error"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        return "dns" if any(hint in text for hint in _DNS_HINTS) else "connect"
    return "dropped"


def record_success(elapsed_sec: float) -> None:
    now = time.time()
    _stats.reads += 1
    _stats.last_ok_at = now
    _stats.latencies.append((now, max(0.0, elapsed_sec) * 1000))


def record_failure(kind: str) -> None:
    if kind not in KIND_LABELS:
        kind = "dropped"
    now = time.time()
    _stats.reads += 1
    _stats.events.append((now, kind))
    _stats.totals[kind] += 1
    _stats.last_failure_at = now
    _stats.last_failure_kind = kind


def latest_failure_kind() -> str | None:
    """Most recent recorded failure kind key (not the Persian label)."""
    return _stats.last_failure_kind


def record_budget_exhaustion() -> None:
    """Count a page-budget timeout separately from panel outages."""
    now = time.time()
    _stats.budget_events.append(now)
    _stats.budget_total += 1


def recent_budget_exhaustions(window_sec: float = WINDOW_SEC) -> int:
    cutoff = time.time() - window_sec
    return sum(1 for moment in _stats.budget_events if moment >= cutoff)


def recent_failures(window_sec: float = WINDOW_SEC) -> Counter[str]:
    cutoff = time.time() - window_sec
    return Counter(kind for moment, kind in _stats.events if moment >= cutoff)


def _percentile(values: list[float], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank percentile.
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index])


def _local_time(moment: float | None) -> str | None:
    return None if moment is None else datetime.fromtimestamp(moment, DISPLAY_ZONE).strftime("%H:%M:%S")


def _iso(moment: float | None) -> str | None:
    return None if moment is None else datetime.fromtimestamp(moment, timezone.utc).isoformat()


def snapshot(circuit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Display data for the owner's connection card."""
    cutoff = time.time() - WINDOW_SEC
    recent_ms = [ms for moment, ms in _stats.latencies if moment >= cutoff]
    recent = recent_failures()
    return {
        "since": _iso(_stats.started_at),
        "reads": _stats.reads,
        "last_ok_at": _iso(_stats.last_ok_at),
        "last_failure_at": _iso(_stats.last_failure_at),
        "last_ok_time": _local_time(_stats.last_ok_at),
        "last_failure_time": _local_time(_stats.last_failure_at),
        "last_failure": KIND_LABELS.get(_stats.last_failure_kind or "", None),
        "latency_p50_ms": _percentile(recent_ms, 0.5),
        "latency_p95_ms": _percentile(recent_ms, 0.95),
        "recent_failures": [
            {"kind": kind, "label": KIND_LABELS[kind], "count": recent[kind], "total": _stats.totals[kind]}
            for kind in KIND_LABELS
            if recent[kind] or _stats.totals[kind]
        ],
        "recent_failure_count": sum(recent.values()),
        "budget_exhaustion_count": recent_budget_exhaustions(),
        "budget_exhaustion_total": _stats.budget_total,
        "budget_exhaustion_label": BUDGET_EXHAUSTION_LABEL,
        "circuit": circuit or {"paused": False, "retry_in_sec": 0, "pause_count": 0},
        "from_prior_boot": False,
        "prior_boot_note": PRIOR_BOOT_NOTE,
    }


def summary_text(window_sec: float = 300.0) -> str:
    """Short ``kind:count`` list for log lines."""
    counts = recent_failures(window_sec)
    return ",".join(f"{kind}:{counts[kind]}" for kind in KIND_LABELS if counts[kind]) or "none"


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _kind_totals_payload() -> str:
    counts = recent_failures(PERSIST_WINDOW_SEC)
    payload = {kind: int(counts[kind]) for kind in KIND_LABELS if counts[kind]}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_kind_totals(raw: str | None) -> Counter[str]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return Counter()
    if not isinstance(data, dict):
        return Counter()
    out: Counter[str] = Counter()
    for kind, value in data.items():
        if kind not in KIND_LABELS:
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[kind] = count
    return out


def memory_is_empty() -> bool:
    """True when this process has not recorded a success or failure yet."""
    return _stats.reads == 0 and _stats.last_ok_at is None and _stats.last_failure_at is None


def merge_persisted_snapshot(
    live: dict[str, Any],
    *,
    last_ok_at: datetime | None,
    last_failure_at: datetime | None,
    last_failure_kind: str | None,
    pause_count: int,
    kind_totals_24h: str | None,
) -> dict[str, Any]:
    """Fill empty in-memory fields from the persisted platform row."""
    out = dict(live)
    prior = False
    ok = _aware(last_ok_at)
    fail = _aware(last_failure_at)
    if out.get("last_ok_at") is None and ok is not None:
        out["last_ok_at"] = _iso(ok.timestamp())
        out["last_ok_time"] = _local_time(ok.timestamp())
        prior = True
    if out.get("last_failure_at") is None and fail is not None:
        out["last_failure_at"] = _iso(fail.timestamp())
        out["last_failure_time"] = _local_time(fail.timestamp())
        kind = last_failure_kind if last_failure_kind in KIND_LABELS else None
        out["last_failure"] = KIND_LABELS.get(kind or "", None)
        prior = True
    circuit = dict(out.get("circuit") or {})
    if not circuit.get("pause_count") and pause_count:
        circuit["pause_count"] = int(pause_count)
        out["circuit"] = circuit
        prior = True
    if not out.get("recent_failures"):
        totals = _parse_kind_totals(kind_totals_24h)
        if totals:
            out["recent_failures"] = [
                {"kind": kind, "label": KIND_LABELS[kind], "count": totals[kind], "total": totals[kind]}
                for kind in KIND_LABELS
                if totals[kind]
            ]
            out["recent_failure_count"] = sum(totals.values())
            prior = True
    out["from_prior_boot"] = prior
    out["prior_boot_note"] = PRIOR_BOOT_NOTE
    return out


async def load_persisted_snapshot(
    session: AsyncSession,
    circuit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Card snapshot: live stats, or persisted summary when memory is empty."""
    live = snapshot(circuit)
    if not memory_is_empty():
        return live
    from app.db.models import PgLinkHealthState

    try:
        row = await session.get(PgLinkHealthState, PLATFORM_ROW_ID)
    except Exception:
        logger.warning("PasarGuard link-health persisted read failed", exc_info=True)
        return live
    if row is None:
        return live
    return merge_persisted_snapshot(
        live,
        last_ok_at=row.last_ok_at,
        last_failure_at=row.last_failure_at,
        last_failure_kind=row.last_failure_kind,
        pause_count=int(row.pause_count or 0),
        kind_totals_24h=row.kind_totals_24h,
    )


async def maybe_persist_link_health(
    session: AsyncSession,
    *,
    pause_count: int | None = None,
    force: bool = False,
) -> bool:
    """Write the platform summary at most once per ``PERSIST_INTERVAL_SEC``. Never on hot reads."""
    global _last_persist_mono
    now_mono = time.monotonic()
    if not force and (now_mono - _last_persist_mono) < PERSIST_INTERVAL_SEC:
        return False
    if memory_is_empty() and not force:
        return False
    from app.db.models import PgLinkHealthState

    now = datetime.now(timezone.utc)
    ok_at = datetime.fromtimestamp(_stats.last_ok_at, timezone.utc) if _stats.last_ok_at else None
    fail_at = (
        datetime.fromtimestamp(_stats.last_failure_at, timezone.utc) if _stats.last_failure_at else None
    )
    kind = _stats.last_failure_kind if _stats.last_failure_kind in KIND_LABELS else None
    totals = _kind_totals_payload()
    pauses = int(pause_count if pause_count is not None else 0)
    row = await session.get(PgLinkHealthState, PLATFORM_ROW_ID)
    if row is None:
        session.add(
            PgLinkHealthState(
                id=PLATFORM_ROW_ID,
                last_ok_at=ok_at,
                last_failure_at=fail_at,
                last_failure_kind=kind,
                pause_count=pauses,
                kind_totals_24h=totals,
                updated_at=now,
            )
        )
    else:
        row.last_ok_at = ok_at or row.last_ok_at
        row.last_failure_at = fail_at or row.last_failure_at
        if kind:
            row.last_failure_kind = kind
        row.pause_count = pauses
        row.kind_totals_24h = totals
        row.updated_at = now
    try:
        await session.commit()
    except Exception:
        logger.warning("PasarGuard link-health persist failed", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            logger.debug("pg_link_health persist rollback failed", exc_info=True)
        return False
    _last_persist_mono = now_mono
    return True
