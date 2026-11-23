"""pgclock status / start / stop / restart / logs / health commands."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from app.cli.context import (
    CliContext,
    SERVICE_NAME,
    env_get,
    service_unit_installed,
    sudo_systemctl,
    systemctl,
    web_port,
)
from app.cli.output import CliError, header, info, kv, ok, warn

FALLBACK_LOG = Path("/tmp/pgclock-panel.log")


def cmd_status(ctx: CliContext) -> int:
    header("status")
    kv("install", ctx.root)
    if ctx.env_path.is_file():
        ok(".env present")
    else:
        warn(".env missing")
    if ctx.venv_python.is_file():
        ok("venv present")
    else:
        warn("venv missing (.venv/bin/python)")

    version = "—"
    ver_file = ctx.root / "VERSION"
    if ver_file.is_file():
        version = ver_file.read_text(encoding="utf-8").strip() or "—"
    kv("version", version)

    if service_unit_installed():
        st = systemctl("is-active", SERVICE_NAME)
        state = (st.stdout or st.stderr or "unknown").strip() or "unknown"
        kv("service", f"{SERVICE_NAME} ({state})")
    else:
        warn(f"systemd unit {SERVICE_NAME} not installed")

    port = web_port(ctx.root)
    health = _probe_health(port)
    if health.get("ok"):
        ok(f"web health :{port} → {health.get('body')}")
    else:
        warn(f"web health :{port} unreachable ({health.get('error')})")

    db = env_get(ctx.root, "DATABASE_URL", "")
    # Only the kind: the URL itself carries the database password.
    if db.startswith(("postgresql://", "postgresql+asyncpg://", "postgres://")):
        kv("database", "postgresql")
    else:
        warn("DATABASE_URL is missing or not PostgreSQL" if db else "DATABASE_URL is missing")
    return 0


def cmd_start(ctx: CliContext) -> int:
    header("start")
    if not service_unit_installed():
        raise CliError(
            f"systemd unit {SERVICE_NAME} not installed. "
            "Run: bash pgclock.sh install   (or service menu → Install systemd unit)"
        )
    proc = sudo_systemctl("start", SERVICE_NAME)
    if proc.returncode != 0:
        raise CliError(
            f"Failed to start {SERVICE_NAME}: {(proc.stderr or proc.stdout or '').strip()}"
        )
    # Best-effort enable for reboot persistence
    sudo_systemctl("enable", SERVICE_NAME)
    ok(f"{SERVICE_NAME} started")
    return 0


def cmd_stop(ctx: CliContext) -> int:
    header("stop")
    if not service_unit_installed():
        raise CliError(f"systemd unit {SERVICE_NAME} not installed")
    proc = sudo_systemctl("stop", SERVICE_NAME)
    if proc.returncode != 0:
        raise CliError(
            f"Failed to stop {SERVICE_NAME}: {(proc.stderr or proc.stdout or '').strip()}"
        )
    ok(f"{SERVICE_NAME} stopped")
    return 0


def cmd_restart(ctx: CliContext) -> int:
    header("restart")
    if not service_unit_installed():
        raise CliError(f"systemd unit {SERVICE_NAME} not installed")
    proc = sudo_systemctl("restart", SERVICE_NAME)
    if proc.returncode != 0:
        raise CliError(
            f"Failed to restart {SERVICE_NAME}: {(proc.stderr or proc.stdout or '').strip()}"
        )
    ok(f"{SERVICE_NAME} restarted")
    return 0


def cmd_logs(ctx: CliContext, *, follow: bool = False, lines: int = 100) -> int:
    header("logs")
    if not service_unit_installed():
        if FALLBACK_LOG.is_file():
            info(f"Showing {FALLBACK_LOG} (no systemd unit)")
            text = FALLBACK_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[
                -max(1, lines) :
            ]
            print("\n".join(text))
            return 0
        raise CliError(
            f"systemd unit {SERVICE_NAME} not installed and no fallback log at {FALLBACK_LOG}"
        )
    if follow:
        return int(subprocess.call(["journalctl", "-u", SERVICE_NAME, "-f"]) or 0)
    proc = subprocess.run(
        ["journalctl", "-u", SERVICE_NAME, "-n", str(max(1, lines)), "--no-pager"],
        check=False,
    )
    return int(proc.returncode or 0)


def cmd_health(ctx: CliContext, *, detail: bool = False) -> int:
    header("health")
    port = web_port(ctx.root)
    basic = _probe_health(port)
    if not basic.get("ok"):
        raise CliError(f"Health check failed on :{port}: {basic.get('error')}")
    ok(f"/health → {basic.get('body')}")
    kv("url", f"http://127.0.0.1:{port}/health")
    if detail:
        info("/health/detail requires an authenticated session — use the web panel.")
    return 0


def _probe_health(port: int) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            return {"ok": True, "body": body, "status": getattr(resp, "status", 200)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
