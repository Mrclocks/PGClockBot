"""Shared CLI context: install root, env loading, service helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.cli.install_root import resolve_install_root
from app.cli.output import CliError

SERVICE_NAME = "pgclockbot"
HELPER_PATH = Path("/usr/local/lib/pgclockbot/ctl")


@dataclass
class CliContext:
    root: Path
    json_mode: bool = False

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def venv_python(self) -> Path:
        return self.root / ".venv" / "bin" / "python"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


def build_context(*, root: str | None = None, json_mode: bool = False) -> CliContext:
    install = resolve_install_root(explicit=root)
    root_s = str(install)
    # Only prepend install root when it actually contains this CLI package
    # (avoids shadowing a complete checkout with an empty scaffold).
    if (install / "app" / "cli").is_dir() and root_s not in sys.path:
        sys.path.insert(0, root_s)
    os.chdir(install)
    os.environ.setdefault("PGCLOCK_HOME", root_s)
    return CliContext(root=install, json_mode=json_mode)


def load_dotenv(root: Path) -> dict[str, str]:
    """Minimal .env reader (no expansion)."""
    path = root / ".env"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def env_get(root: Path, key: str, default: str = "") -> str:
    """Read config for CLI ops — install .env wins over ambient shell env.

    The global CLI must reflect the target install's configuration when invoked
    from any directory. Ambient DATABASE_URL/WEB_PORT from the caller's shell
    must not silently redirect operations.
    """
    file_vals = load_dotenv(root)
    if key in file_vals and str(file_vals[key]).strip() != "":
        return str(file_vals[key]).strip()
    if key in os.environ and str(os.environ.get(key) or "").strip() != "":
        return str(os.environ[key]).strip()
    return default


def run_cmd(
    cmd: list[str],
    *,
    timeout: float | None = 60,
    check: bool = False,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def service_unit_installed() -> bool:
    return Path(f"/etc/systemd/system/{SERVICE_NAME}.service").is_file() or Path(
        f"/lib/systemd/system/{SERVICE_NAME}.service"
    ).is_file()


def systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return run_cmd(["systemctl", *args], timeout=timeout)


def sudo_systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    # Prefer constrained helper when present for single-verb ops
    helper_verbs = {"start", "stop", "restart", "is-active", "enable", "disable", "status"}
    if len(args) == 1 and args[0] in helper_verbs and HELPER_PATH.is_file():
        proc = run_cmd(["sudo", "-n", str(HELPER_PATH), args[0]], timeout=timeout)
        if proc.returncode == 0 or args[0] == "is-active":
            return proc
    proc = run_cmd(["sudo", "-n", "systemctl", *args], timeout=timeout)
    if proc.returncode == 0:
        return proc
    return systemctl(*args, timeout=timeout)


def require_confirm(prompt: str, *, yes: bool) -> None:
    if yes:
        return
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in {"y", "yes"}:
        raise CliError("Aborted.", code=2)


def web_port(root: Path) -> int:
    raw = env_get(root, "WEB_PORT", "9000") or "9000"
    try:
        return int(raw)
    except ValueError:
        return 9000


def database_url(root: Path) -> str:
    url = env_get(root, "DATABASE_URL", "")
    if not url:
        raise CliError("DATABASE_URL is not set in .env (PostgreSQL required).")
    return url
