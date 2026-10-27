"""Reliable panel service restart helpers for SSL / updates / bot settings.

Priority:
1) passwordless sudo helper / systemctl (clean systemd restart)
2) under systemd: exit so Restart=always brings the unit back (no sudo)
3) in-place re-exec of the same Python process (no sudo)
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, ROOT_DIR

logger = logging.getLogger(__name__)

SERVICE_NAME = "pgclockbot"
STATUS_PATH = DATA_DIR / "restart_status.json"
HELPER_INSTALL_PATH = Path("/usr/local/lib/pgclockbot/ctl")
HELPER_SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "pgclockbot-ctl"
SUDOERS_PATH = Path("/etc/sudoers.d/pgclockbot")

_restart_lock = threading.Lock()
_restart_scheduled = False


def _write_status(payload: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("restart status write failed", exc_info=True)


def service_user() -> str:
    return (
        os.environ.get("PGCLOCKBOT_SERVICE_USER")
        or os.environ.get("SUDO_USER")
        or os.environ.get("USER")
        or "root"
    ).strip() or "root"


def _run(cmd: list[str], *, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def can_passwordless_sudo() -> bool:
    try:
        return _run(["sudo", "-n", "true"], timeout=5).returncode == 0
    except Exception:
        return False


def under_systemd_service() -> bool:
    """True when this process is a systemd unit (INVOCATION_ID is set by systemd)."""
    if os.environ.get("INVOCATION_ID"):
        return True
    if os.environ.get("NOTIFY_SOCKET"):
        return True
    # Fallback: parent is systemd
    try:
        ppid = os.getppid()
        comm = Path(f"/proc/{ppid}/comm").read_text(encoding="utf-8").strip()
        return comm == "systemd"
    except Exception:
        return False


def _installed_helper_is_current() -> bool:
    """The installed helper matches this release's copy (an update may add operations)."""
    try:
        return HELPER_INSTALL_PATH.read_bytes() == HELPER_SOURCE.read_bytes()
    except OSError:
        # Unreadable or missing: let the caller reinstall when it can.
        return False


def helper_capabilities() -> frozenset[str]:
    """Optional operations the installed helper supports; empty for helpers that predate them."""
    if not HELPER_INSTALL_PATH.is_file():
        return frozenset()
    try:
        result = _run(["sudo", "-n", str(HELPER_INSTALL_PATH), "capabilities"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        logger.warning("helper capabilities probe failed", exc_info=True)
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset((result.stdout or "").split())


def ensure_restart_helper() -> tuple[bool, str]:
    """Install ctl helper + sudoers so the service user can restart without a password.

    An installed helper from an older release is replaced when sudo allows it, so new
    operations (a second certificate name, for one) are available after an update.
    """
    if HELPER_INSTALL_PATH.is_file() and SUDOERS_PATH.is_file():
        # An older helper that cannot be replaced still restarts the service as before.
        if (_installed_helper_is_current() or not can_passwordless_sudo()) and _sudo_restart_works():
            return True, "helper آماده است"

    if not HELPER_SOURCE.is_file():
        return False, "فایل کمکی ریستارت پیدا نشد (scripts/pgclockbot-ctl)."

    if not can_passwordless_sudo():
        return False, (
            "sudo بدون پسورد در دسترس نیست — از مسیر جایگزین (systemd Restart=always / re-exec) استفاده می‌شود."
        )

    user = service_user()
    try:
        with tempfile.TemporaryDirectory(prefix="pgclock-ctl-") as tmp:
            tmp_path = Path(tmp)
            ctl_copy = tmp_path / "ctl"
            shutil.copy2(HELPER_SOURCE, ctl_copy)
            ctl_copy.chmod(0o755)

            sudoers = tmp_path / "sudoers"
            sudoers.write_text(
                "# Managed by PGClockBot — passwordless service control via constrained helper only\n"
                f"{user} ALL=(root) NOPASSWD: {HELPER_INSTALL_PATH} *\n"
                f"{user} ALL=(root) NOPASSWD: "
                f"/bin/systemctl start {SERVICE_NAME}, "
                f"/usr/bin/systemctl start {SERVICE_NAME}, "
                f"/bin/systemctl stop {SERVICE_NAME}, "
                f"/usr/bin/systemctl stop {SERVICE_NAME}, "
                f"/bin/systemctl restart {SERVICE_NAME}, "
                f"/usr/bin/systemctl restart {SERVICE_NAME}, "
                f"/bin/systemctl try-restart {SERVICE_NAME}, "
                f"/usr/bin/systemctl try-restart {SERVICE_NAME}, "
                f"/bin/systemctl enable {SERVICE_NAME}, "
                f"/usr/bin/systemctl enable {SERVICE_NAME}, "
                f"/bin/systemctl is-active {SERVICE_NAME}, "
                f"/usr/bin/systemctl is-active {SERVICE_NAME}\n",
                encoding="utf-8",
            )
            sudoers.chmod(0o440)

            install_script = tmp_path / "install.sh"
            install_script.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                f"install -d -m 755 {shlex.quote(str(HELPER_INSTALL_PATH.parent))}\n"
                f"install -m 755 {shlex.quote(str(ctl_copy))} {shlex.quote(str(HELPER_INSTALL_PATH))}\n"
                f"install -m 440 {shlex.quote(str(sudoers))} {shlex.quote(str(SUDOERS_PATH))}\n"
                f"visudo -cf {shlex.quote(str(SUDOERS_PATH))}\n",
                encoding="utf-8",
            )
            install_script.chmod(0o700)

            result = _run(["sudo", "-n", "bash", str(install_script)], timeout=30)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
                return False, f"نصب helper ریستارت ناموفق: {err[:240]}"
        if _sudo_restart_works():
            return True, "helper ریستارت نصب شد"
        return False, "helper نصب شد ولی هنوز اجازهٔ ریستارت تأیید نشد"
    except Exception as exc:
        return False, f"نصب helper ریستارت ناموفق: {exc}"


def _sudo_failed(result: subprocess.CompletedProcess[str]) -> bool:
    err = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()
    if "a password is required" in err or "password is required" in err:
        return True
    if "no password was provided" in err:
        return True
    if "authentication is required" in err:
        return True
    if result.returncode in {1, 127} and "sudo:" in err and "password" in err:
        return True
    return False


def _sudo_restart_works() -> bool:
    """Probe whether passwordless restart is actually allowed."""
    probes = [
        ["sudo", "-n", str(HELPER_INSTALL_PATH), "is-active"],
        ["sudo", "-n", "systemctl", "is-active", SERVICE_NAME],
        ["sudo", "-n", "/bin/systemctl", "is-active", SERVICE_NAME],
        ["sudo", "-n", "/usr/bin/systemctl", "is-active", SERVICE_NAME],
    ]
    for cmd in probes:
        try:
            if cmd[2].startswith("/") and not Path(cmd[2]).exists() and "systemctl" not in cmd[2]:
                continue
            if "pgclockbot/ctl" in cmd[2] and not Path(cmd[2]).is_file():
                continue
            r = _run(cmd, timeout=5)
            if _sudo_failed(r):
                continue
            # is-active: 0=active, 3=inactive/failed — both mean sudo worked
            out = (r.stdout or "").strip().lower()
            if out in {"active", "inactive", "activating", "deactivating", "failed", "reloading"}:
                return True
            if r.returncode in {0, 3, 4}:
                return True
        except Exception:
            continue
    return False


def _detached_restart(cmd: list[str], *, delay_sec: float) -> bool:
    """Fire-and-forget restart so the current HTTP worker can exit cleanly."""
    delay = max(0.8, float(delay_sec))
    delay_str = f"{delay:.1f}"
    inner = cmd[2:] if cmd[:2] == ["sudo", "-n"] else cmd

    try:
        probe = _run(["systemd-run", "--version"], timeout=3)
        if probe.returncode == 0:
            for prefix in (["sudo", "-n"], []):
                wrapped = [
                    *prefix,
                    "systemd-run",
                    "--quiet",
                    "--collect",
                    f"--on-active={delay_str}",
                    "--timer-property=AccuracySec=100ms",
                    *inner,
                ]
                r = _run(wrapped, timeout=10)
                if r.returncode == 0 and not _sudo_failed(r):
                    return True
    except Exception:
        logger.debug("systemd-run restart schedule failed", exc_info=True)

    try:
        # Keep the delayed job outside this service cgroup when possible.
        shell_cmd = (
            f"sleep {delay_str}; "
            + " ".join(shlex.quote(c) for c in cmd)
            + f" || sleep 1; "
            + " ".join(shlex.quote(c) for c in cmd)
        )
        subprocess.Popen(
            ["bash", "-lc", shell_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception:
        logger.exception("detached restart spawn failed")
        return False


def _reexec_argv() -> list[str]:
    run_py = ROOT_DIR / "run.py"
    exe = sys.executable or "python3"
    if run_py.is_file():
        return [exe, str(run_py)]
    argv0 = sys.argv[0] if sys.argv else str(run_py)
    return [exe, argv0, *sys.argv[1:]]


def _schedule_self_restart(*, delay_sec: float, reason: str) -> tuple[bool, str]:
    """Restart without sudo: systemd Restart=always, else in-place re-exec."""
    delay = max(1.0, float(delay_sec))
    use_systemd = under_systemd_service()
    method = "systemd-exit" if use_systemd else "reexec"

    def _worker() -> None:
        try:
            time.sleep(delay)
            _write_status(
                {
                    "state": "executing",
                    "reason": reason,
                    "method": method,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "pid": os.getpid(),
                }
            )
            if use_systemd:
                # Restart=always will spawn a fresh unit with the new code.
                logger.warning("Exiting for systemd Restart=always (%s)", reason)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(2.0)
                os._exit(0)
            argv = _reexec_argv()
            logger.warning("Re-exec panel: %s (%s)", argv, reason)
            os.chdir(str(ROOT_DIR))
            os.execvp(argv[0], argv)
        except Exception:
            logger.exception("self restart failed")
            try:
                os._exit(1)
            except Exception:
                pass

    # Non-daemon so the process stays alive until restart fires
    threading.Thread(target=_worker, name="pgclock-self-restart", daemon=False).start()
    if use_systemd:
        return True, "ریستارت از طریق systemd زمان‌بندی شد (بدون نیاز به sudo)."
    return True, "ری‌استارت داخلی پنل زمان‌بندی شد (بارگذاری دوبارهٔ کد)."


def restart_panel_service(*, reason: str = "manual", delay_sec: float = 0.0) -> tuple[bool, str]:
    """Schedule a service restart and return immediately."""
    delay = float(delay_sec or 1.5)
    _write_status(
        {
            "state": "scheduled",
            "reason": reason,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pid": os.getpid(),
            "delay_sec": delay,
        }
    )

    # Prefer a real systemctl restart when passwordless sudo is available.
    if _sudo_restart_works() or can_passwordless_sudo():
        candidates: list[list[str]] = []
        if HELPER_INSTALL_PATH.is_file():
            candidates.append(["sudo", "-n", str(HELPER_INSTALL_PATH), "restart"])
        candidates.extend(
            [
                ["sudo", "-n", "systemctl", "restart", SERVICE_NAME],
                ["sudo", "-n", "/bin/systemctl", "restart", SERVICE_NAME],
                ["sudo", "-n", "/usr/bin/systemctl", "restart", SERVICE_NAME],
            ]
        )
        if HELPER_SOURCE.is_file():
            candidates.append(["sudo", "-n", str(HELPER_SOURCE), "restart"])
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                candidates.append(["systemctl", "restart", SERVICE_NAME])
        except Exception:
            pass

        for cmd in candidates:
            if cmd[:2] == ["sudo", "-n"] and not (
                _sudo_restart_works() or can_passwordless_sudo()
            ):
                continue
            if _detached_restart(cmd, delay_sec=delay):
                logger.info("panel restart scheduled via %s (%s)", " ".join(cmd), reason)
                _write_status(
                    {
                        "state": "scheduled",
                        "reason": reason,
                        "method": "systemctl",
                        "cmd": " ".join(cmd),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "delay_sec": delay,
                    }
                )
                return True, "ریستارت سرویس زمان‌بندی شد. چند ثانیه صبر کنید."

    # No sudo needed — works on normal systemd installs (Restart=always).
    ok, note = _schedule_self_restart(delay_sec=delay, reason=reason)
    if ok:
        return True, note

    _write_status(
        {
            "state": "failed",
            "reason": reason,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": "no restart method available",
        }
    )
    return False, (
        "ریستارت خودکار در دسترس نیست. روی سرور اجرا کنید: "
        f"sudo systemctl restart {SERVICE_NAME}"
    )


def schedule_panel_restart(
    *,
    delay_sec: float = 2.5,
    reason: str = "config change",
    ensure_helper: bool = True,
) -> bool:
    """Schedule a one-shot restart after the HTTP response can flush.

    Returns True if a restart was newly scheduled.
    """
    global _restart_scheduled
    with _restart_lock:
        if _restart_scheduled:
            return False
        _restart_scheduled = True

    if ensure_helper:
        ok_h, msg_h = ensure_restart_helper()
        if not ok_h:
            logger.warning("restart helper setup: %s", msg_h)

    ok, note = restart_panel_service(reason=reason, delay_sec=delay_sec)
    if not ok:
        with _restart_lock:
            _restart_scheduled = False
        logger.error("Panel restart schedule failed (%s): %s", reason, note)
        return False

    def _clear() -> None:
        global _restart_scheduled
        time.sleep(max(8.0, float(delay_sec) + 5.0))
        with _restart_lock:
            _restart_scheduled = False

    threading.Thread(target=_clear, name="pgclock-restart-flag", daemon=True).start()
    logger.warning("Panel restart scheduled in %.1fs (%s): %s", delay_sec, reason, note)
    return True
