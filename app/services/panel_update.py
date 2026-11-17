from __future__ import annotations

"""In-panel self-update with persisted progress for the web UI."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, ROOT_DIR
from app.services.connection_monitor_install_update import setup_updated_monitor_core
from app.services.panel_update_archive import (
    archive_url,
    download_archive,
    extract_archive,
    new_staging_dir,
    overlay,
)
from app.services.panel_update_guard import (
    RollbackPoint,
    UpdateStepError,
    backup_database,
    capture_rollback_point,
    install_requirements,
    migrate_schema,
    python_for,
    read_schema_revision,
    requirements_at,
    switch_code,
    verify_new_code,
    write_boot_marker,
)
from app.services.updates import check_github_update, clear_update_cache, is_newer, local_version

logger = logging.getLogger(__name__)

STATUS_FILE = DATA_DIR / "panel_update.json"
ROLLED_BACK_MESSAGE = (
    "آپدیت انجام نشد: نسخهٔ جدید بررسی‌ها را رد کرد. نسخهٔ قبلی برگردانده شد و پنل بدون وقفه کار می‌کند."
)
_LOCK = threading.RLock()
_THREAD: threading.Thread | None = None

# systemd often has a short PATH — resolve absolute binaries
from app.util_which import env_with_path, which as _which


def _repo_root() -> Path:
    """Prefer ROOT_DIR; walk up if .git lives in a parent (unusual installs)."""
    candidates = [ROOT_DIR, Path.cwd()]
    for base in candidates:
        try:
            cur = base.resolve()
        except Exception:
            cur = base
        for _ in range(6):
            if (cur / ".git").exists() and (cur / "requirements.txt").exists():
                return cur
            if (cur / ".git").exists() and (cur / "run.py").exists():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
    return ROOT_DIR.resolve()


def _env_with_path() -> dict[str, str]:
    return env_with_path(
        DEBIAN_FRONTEND="noninteractive",
        GIT_TERMINAL_PROMPT="0",
    )

STEPS = [
    ("prepare", "آماده‌سازی", 5),
    ("backup", "پشتیبان‌گیری", 15),
    ("fetch", "ارتباط با گیت‌هاب", 30),
    ("deps", "نصب وابستگی‌ها", 50),
    ("pull", "اعمال کد", 65),
    ("check", "بررسی نسخهٔ جدید", 80),
    ("restart", "راه‌اندازی مجدد سرویس", 95),
    ("done", "تمام شد", 100),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_status() -> dict[str, Any]:
    return {
        "state": "idle",  # idle | running | done | error
        "percent": 0,
        "step": "",
        "step_key": "",
        "message": "آماده برای آپدیت",
        "log": [],
        "started_at": None,
        "finished_at": None,
        "from_version": local_version(),
        "to_version": None,
        "error": None,
        "awaiting_restart": False,
        "restart_required": False,
        "pre_boot_id": None,
    }


def _replace_status(data: dict[str, Any]) -> dict[str, Any]:
    """Overwrite status file entirely (does not merge with previous)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)
        return data


def read_status() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATUS_FILE.exists():
        return _default_status()
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_status()
        base = _default_status()
        base.update(data)
        return base
    except Exception:
        return _default_status()


def write_status(patch: dict[str, Any]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        cur = read_status()
        cur.update(patch)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)
        return cur


def _append_log(line: str) -> None:
    with _LOCK:
        cur = read_status()
        log = list(cur.get("log") or [])
        log.append(f"{datetime.now().astimezone().strftime('%H:%M:%S')} {line}")
        cur["log"] = log[-100:]
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATUS_FILE)


def _set_step(key: str, message: str | None = None) -> None:
    meta = next((s for s in STEPS if s[0] == key), None)
    if not meta:
        return
    write_status(
        {
            "state": "running" if key != "done" else "done",
            "step_key": key,
            "step": meta[1],
            "percent": meta[2],
            "message": message or meta[1],
        }
    )
    _append_log(message or meta[1])


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 300) -> tuple[int, str]:
    try:
        # Resolve first token if it's a bare command name
        exe = cmd[0]
        if "/" not in exe and not Path(exe).exists():
            resolved = _which(exe)
            if not resolved:
                return 1, f"دستور «{exe}» روی سرور پیدا نشد (PATH ناقص است)"
            cmd = [resolved, *cmd[1:]]
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or _repo_root()),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env_with_path(),
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except FileNotFoundError as e:
        return 1, f"فایل/دستور پیدا نشد: {e.filename or e}"
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:
        return 1, str(e)


def _restart_service() -> tuple[bool, str]:
    """Schedule an automatic restart using the freshest on-disk code possible."""
    note = ""

    # 1) Prefer a fresh interpreter process so we never use a stale imported module
    #    after git pull / archive overlay during in-panel update.
    try:
        script = ROOT_DIR / "scripts" / "panel_restart.py"
        py = sys.executable or "python3"
        if script.is_file():
            proc = subprocess.Popen(
                [py, str(script), "--delay", "3.5", "--reason", "panel update"],
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env=_env_with_path(),
            )
            try:
                out_b, _ = proc.communicate(timeout=8)
                out = (out_b or b"").decode("utf-8", errors="replace").strip()
            except subprocess.TimeoutExpired:
                # Still running — restart was likely armed; do not kill it.
                out = "restart helper still running"
            if out:
                _append_log(out.splitlines()[-1][:240])
            if proc.poll() in (None, 0):
                return True, out.splitlines()[-1] if out else "ریستارت سرویس زمان‌بندی شد"
            note = out or f"panel_restart exit {proc.returncode}"
            _append_log(note[:240])
    except Exception as exc:
        logger.exception("panel_restart.py spawn failed")
        note = str(exc)
        _append_log(f"spawn restart script: {note[:200]}")

    # 2) Same-process helpers (reload from disk)
    try:
        import importlib

        from app.services import service_control as sc

        sc = importlib.reload(sc)
        helper_ok, helper_msg = sc.ensure_restart_helper()
        if helper_msg:
            _append_log(helper_msg)
        ok, note2 = sc.restart_panel_service(reason="panel update", delay_sec=3.5)
        if ok:
            return True, note2
        note = note2 or note
        _append_log(note or "ریستارت از service_control ناموفق")
    except Exception as exc:
        logger.exception("panel update restart failed")
        note = str(exc)
        _append_log(f"خطا در ریستارت: {note[:200]}")

    # 3) Hard fallback: under systemd Restart=always, exiting the process is enough.
    try:
        if os.environ.get("INVOCATION_ID") or os.environ.get("NOTIFY_SOCKET"):

            def _die() -> None:
                time.sleep(3.5)
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except Exception:
                    logger.debug("_die: ignored failure", exc_info=True)
                os._exit(0)

            threading.Thread(target=_die, name="panel-update-die", daemon=False).start()
            return True, "ریستارت از طریق systemd زمان‌بندی شد"
    except Exception as exc:
        _append_log(f"fallback ریستارت ناموفق: {exc}")

    return False, note or (
        "ریستارت خودکار ممکن نشد — روی سرور: sudo systemctl restart pgclockbot"
    )


def _git_bin() -> str | None:
    return _which("git")


def _ensure_git() -> str | None:
    """Locate git; if missing and we are root, try apt install once."""
    git = _git_bin()
    if git:
        return git
    try:
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        is_root = False
    if not is_root:
        return None
    apt = _which("apt-get")
    if not apt:
        return None
    _append_log("git پیدا نشد — تلاش برای نصب با apt…")
    code, out = _run([apt, "install", "-y", "git"], timeout=300)
    if code != 0:
        _append_log((out or "apt install git ناموفق")[:200])
        return None
    git = _git_bin()
    if git:
        _append_log(f"git نصب شد: {git}")
    return git


def _looks_like_version(value: str | None) -> bool:
    from app.services.updates import normalize_version_tag

    return bool(normalize_version_tag(value))


def _read_local_version_file(root: Path) -> str | None:
    ver_file = root / "VERSION"
    if not ver_file.exists():
        return None
    try:
        return ver_file.read_text(encoding="utf-8").strip().splitlines()[0].strip() or None
    except Exception:
        return None


def _finish_ok(message: str, *, awaiting_restart: bool = False, restart_required: bool = False) -> None:
    pre_boot = None
    if awaiting_restart:
        try:
            from app.runtime import BOOT_ID

            pre_boot = BOOT_ID
        except Exception:
            pre_boot = None
    write_status(
        {
            "state": "error" if restart_required else "done",
            "percent": 100,
            "step_key": "done" if not awaiting_restart else "restart",
            "step": "تمام شد" if not awaiting_restart else "راه‌اندازی مجدد سرویس",
            "message": message,
            "finished_at": _now(),
            "error": message if restart_required else None,
            "awaiting_restart": bool(awaiting_restart),
            "restart_required": bool(restart_required),
            "pre_boot_id": pre_boot,
        }
    )
    _append_log(message)
    clear_update_cache()


def _finish_error(err: Exception | str) -> None:
    msg = str(err)
    if isinstance(err, Exception):
        logger.exception("panel update failed: %s", msg)
    else:
        logger.error("panel update failed: %s", msg)
    write_status(
        {
            "state": "error",
            "message": msg,
            "error": msg,
            "finished_at": _now(),
            "awaiting_restart": False,
        }
    )
    _append_log(f"خطا: {msg}")


def _start_running_status(target_version: str | None) -> None:
    write_status(
        {
            "state": "running",
            "percent": 0,
            "error": None,
            "finished_at": None,
            "started_at": _now(),
            "from_version": local_version(),
            "to_version": target_version,
            "log": [],
            "awaiting_restart": False,
            "restart_required": False,
            "pre_boot_id": None,
            "step": "",
            "step_key": "",
            "message": "شروع آپدیت…",
        }
    )


def _backup_before_update(root: Path) -> str:
    """Copy .env and take a full backup; nothing has changed yet if this fails."""
    _set_step("backup", "پشتیبان‌گیری کامل (دیتابیس و تنظیمات)…")
    env_path = root / ".env"
    if env_path.exists():
        bak = root / f".env.bak.{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(env_path, bak)
        _append_log(f"پشتیبان env: {bak.name}")
    return backup_database(log=_append_log)


def _reinstall_current_requirements(root: Path, python: str) -> None:
    req = root / "requirements.txt"
    if not req.is_file():
        return
    try:
        install_requirements(root, req.read_bytes(), python=python, runner=_run, log=_append_log)
    except UpdateStepError as exc:
        # The previous code usually runs on the newer packages; say so, do not fail the rollback.
        _append_log(f"بازگرداندن پکیج‌های نسخهٔ قبلی کامل نشد: {exc}")


def _migrate_or_explain(root: Path, python: str, schema_before: str | None, backup_id: str) -> bool:
    """Run migrations; ``False`` means the schema is unchanged and rollback is safe."""
    try:
        migrate_schema(root, python=python, runner=_run)
        return True
    except UpdateStepError as exc:
        if schema_before is not None and read_schema_revision() == schema_before:
            _append_log(str(exc))
            return False
        raise UpdateStepError(
            f"{exc} — ساختار دیتابیس تغییر کرده و نسخهٔ قبلی برگردانده نشد. "
            f"برای برگشت، پشتیبان {backup_id or 'قبل از آپدیت'} را از تنظیمات › پشتیبان بازگردانید."
        ) from exc


def _check_new_code(root: Path, python: str, schema_before: str | None, backup_id: str) -> bool:
    """Import check, then migrations, all before the restart. ``False`` = roll back."""
    _set_step("check", "بررسی نسخهٔ جدید و به‌روزرسانی دیتابیس…")
    try:
        verify_new_code(root, python=python, runner=_run)
    except UpdateStepError as exc:
        _append_log(str(exc))
        return False
    return _migrate_or_explain(root, python, schema_before, backup_id)


def _fetch_channel_tip(git: str, root: Path, branch: str) -> str | None:
    _set_step("fetch", f"git fetch origin {branch}…")
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    code, out = _run([git, "fetch", "--no-tags", "origin", refspec], cwd=root, timeout=120)
    if code != 0:
        _append_log(f"git fetch ناموفق — سوییچ به zip: {(out or '')[:160]}")
        return None
    code, out = _run([git, "rev-parse", "--verify", f"origin/{branch}^{{commit}}"], cwd=root, timeout=30)
    if code != 0 or not out.strip():
        _append_log(f"origin/{branch} پیدا نشد — سوییچ به zip")
        return None
    return out.strip().splitlines()[-1]


def _roll_back_git(git: str, root: Path, point: RollbackPoint, python: str) -> None:
    try:
        switch_code(git, root, commit=point.commit, branch=point.branch, runner=_run)
    except UpdateStepError as exc:
        raise UpdateStepError(
            f"برگرداندن نسخهٔ قبلی ناموفق بود ({exc}). روی سرور: bash pgclock.sh و گزینهٔ Update"
        ) from exc
    _reinstall_current_requirements(root, python)
    _append_log(f"نسخهٔ قبلی برگردانده شد ({point.commit[:12]})")


def _apply_with_git(git: str, root: Path, branch: str, backup_id: str) -> bool:
    """Update through git. ``False`` when GitHub is unreachable over git (try zip)."""
    python = python_for(root)
    point = capture_rollback_point(git, root, runner=_run)
    target = _fetch_channel_tip(git, root, branch)
    if target is None:
        return False
    _set_step("deps", "نصب پکیج‌های نسخهٔ جدید (قبل از تعویض کد)…")
    requirements = requirements_at(git, root, target, runner=_run)
    if requirements:
        install_requirements(root, requirements, python=python, runner=_run, log=_append_log)
    _set_step("pull", f"اعمال کد (origin/{branch})…")
    try:
        switch_code(git, root, commit=target, branch=branch, start=f"origin/{branch}", runner=_run)
    except UpdateStepError:
        _roll_back_git(git, root, point, python)
        raise
    _append_log(f"کد با origin/{branch} همگام شد ({target[:12]})")
    if not _check_new_code(root, python, point.schema, backup_id):
        _roll_back_git(git, root, point, python)
        raise UpdateStepError(ROLLED_BACK_MESSAGE)
    write_boot_marker(point, new_commit=target, backup_id=backup_id)
    return True


def _apply_with_archive(root: Path, branch: str, backup_id: str) -> None:
    """Update from the GitHub zip; replaced files are restored if a check fails."""
    from app.version import GITHUB_REPO

    python = python_for(root)
    schema_before = read_schema_revision()
    _set_step("fetch", "دانلود نسخه جدید…")
    url, label = archive_url(GITHUB_REPO, branch, is_version=_looks_like_version(branch))
    _append_log(f"دانلود آرشیو گیت‌هاب ({label})…")
    data = download_archive(url)
    with new_staging_dir() as tmp:
        staging = Path(tmp)
        src = extract_archive(data, staging)
        requirements = src / "requirements.txt"
        if requirements.is_file():
            _set_step("deps", "نصب پکیج‌های نسخهٔ جدید (قبل از تعویض کد)…")
            install_requirements(root, requirements.read_bytes(), python=python, runner=_run, log=_append_log)
        _set_step("pull", "اعمال فایل‌ها…")
        snapshot = overlay(src, root, staging / "previous", log=_append_log)
        if not _check_new_code(root, python, schema_before, backup_id):
            snapshot.restore()
            _reinstall_current_requirements(root, python)
            raise UpdateStepError(ROLLED_BACK_MESSAGE)
    _append_log("کد از آرشیو گیت‌هاب اعمال شد")


def _restart_after_update(root: Path, target_version: str | None) -> None:
    write_status({"to_version": _read_local_version_file(root) or target_version})
    _set_step("restart", "راه‌اندازی مجدد…")
    ok, note = _restart_service()
    _append_log(note)
    if not ok:
        _finish_ok(
            "کد آپدیت شد؛ ریستارت خودکار ممکن نشد. "
            "یک‌بار روی سرور: sudo systemctl restart pgclockbot",
            restart_required=True,
        )
        return
    time.sleep(0.4)
    _finish_ok(
        "آپدیت انجام شد — سرویس در حال راه‌اندازی مجدد است. "
        "ممکن است چند دقیقه طول بکشد؛ از صفحه خارج نشوید و رفرش نکنید. صفحه به‌صورت خودکار تازه می‌شود.",
        awaiting_restart=True,
    )


def _do_update(target_version: str | None) -> None:
    """Update order: backup → packages → code → import/migration check → restart.

    Every step before the restart runs while the current panel keeps serving; a
    failure there puts the previous code back, so the panel never goes down.
    """
    from app.services.update_channel import (
        channel_branch,
        channel_label_fa,
        get_update_channel,
        set_deployed_channel,
    )

    try:
        root = _repo_root()
        _start_running_status(target_version)
        _set_step("prepare", "شروع آپدیت…")
        _append_log(f"root={root}")
        git = _ensure_git()
        _append_log(f"git={git}" if git else "git در دسترس نیست — از آرشیو zip استفاده می‌شود")
        channel = get_update_channel()
        branch = channel_branch(channel)
        _append_log(f"کانال آپدیت: {channel_label_fa(channel)} ({branch})")

        backup_id = _backup_before_update(root)
        applied = bool(git and (root / ".git").exists()) and _apply_with_git(git, root, branch, backup_id)
        if not applied:
            _apply_with_archive(root, branch, backup_id)

        # Code tip is now this channel — badge follows DEPLOYED_CHANNEL after restart.
        set_deployed_channel(channel)
        _append_log(f"کانال نصب‌شده: {channel_label_fa(channel)}")
        setup_updated_monitor_core(root, runner=_run, log=_append_log)
        _restart_after_update(root, target_version)
    except UpdateStepError as e:
        _finish_error(str(e))
    except Exception as e:
        _finish_error(e)

def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _age_seconds(iso_ts: str | None) -> float | None:
    ts = _parse_iso(iso_ts)
    if ts is None:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - ts)


def _thread_alive() -> bool:
    t = _THREAD
    return bool(t is not None and t.is_alive())


def _start_update_thread(target_version: str | None) -> dict[str, Any]:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return {"ok": False, "error": "یک عملیات آپدیت در حال اجراست"}
        # Mark running immediately so /update/status never returns stale awaiting.
        seed = _default_status()
        seed.update(
            {
                "state": "running",
                "started_at": _now(),
                "message": "شروع عملیات…",
                "awaiting_restart": False,
                "restart_required": False,
                "pre_boot_id": None,
                "log": [],
                "to_version": target_version,
            }
        )
        _replace_status(seed)
        _THREAD = threading.Thread(target=_do_update, args=(target_version,), name="panel-update", daemon=True)
        _THREAD.start()
    return {"ok": True, "status": read_status()}


def clear_idle_status() -> dict[str, Any]:
    """Reset progress UI when nothing active remains."""
    st = read_status()
    if st.get("state") == "running" and _thread_alive():
        return st
    return _replace_status(_default_status())


# How long to wait for systemd restart before unlocking the UI for retry.
AWAITING_RESTART_TIMEOUT_SEC = 12 * 60
STALE_RUNNING_TIMEOUT_SEC = 20 * 60


def resolve_stale_update_status() -> dict[str, Any]:
    """
    Recover stuck panel_update.json so the update UI stays usable.

    - Clear awaiting_restart only after a real process identity change, or timeout.
    - Unlock zombie "running" when the worker thread is gone.
    - Keep restart_required / error visible until cleared.
    """
    from app.services.updates import is_same_or_newer

    st = read_status()
    local = local_version()
    to_ver = str(st.get("to_version") or "").strip()

    # Zombie running (process restarted mid-update, thread gone).
    if st.get("state") == "running":
        if _thread_alive():
            return st
        age = _age_seconds(st.get("started_at")) or _age_seconds(st.get("finished_at"))
        if age is None or age >= 30:
            return write_status(
                {
                    "state": "error",
                    "awaiting_restart": False,
                    "restart_required": False,
                    "finished_at": _now(),
                    "message": "عملیات آپدیت ناتمام ماند. دوباره تلاش کنید.",
                    "error": "عملیات آپدیت ناتمام ماند. دوباره تلاش کنید.",
                    "step_key": "restart",
                }
            )
        return st

    if st.get("restart_required"):
        # Keep actionable manual-restart message visible.
        return st

    if st.get("state") == "error":
        return st

    awaiting = bool(st.get("awaiting_restart"))
    if awaiting:
        try:
            from app.runtime import BOOT_ID

            cur_boot = BOOT_ID
        except Exception:
            cur_boot = None
        pre_boot = st.get("pre_boot_id")

        restarted = bool(pre_boot and cur_boot and str(pre_boot) != str(cur_boot))
        version_ok = is_same_or_newer(local, to_ver) if to_ver else restarted

        if restarted and version_ok:
            return clear_idle_status()

        # Legacy status without pre_boot_id: if already on/after target, clear.
        if not pre_boot and to_ver and is_same_or_newer(local, to_ver):
            age = _age_seconds(st.get("finished_at"))
            if age is not None and age >= 20:
                return clear_idle_status()

        age = _age_seconds(st.get("finished_at")) or _age_seconds(st.get("started_at"))
        if age is not None and age >= AWAITING_RESTART_TIMEOUT_SEC:
            return write_status(
                {
                    "state": "error",
                    "awaiting_restart": False,
                    "restart_required": True,
                    "finished_at": _now(),
                    "step_key": "restart",
                    "step": "راه‌اندازی مجدد سرویس",
                    "message": (
                        "ری‌استارت بیش از حد طول کشید. اگر نسخه عوض نشده، روی سرور اجرا کنید: "
                        "sudo systemctl restart pgclockbot — سپس دوباره بررسی کنید."
                    ),
                    "error": (
                        "ری‌استارت بیش از حد طول کشید. اگر لازم است: "
                        "sudo systemctl restart pgclockbot"
                    ),
                }
            )
        return st

    # Finished cleanly (no awaiting) — hide logs/steps.
    if st.get("state") == "done" or st.get("log") or st.get("step_key"):
        return clear_idle_status()
    return st


def start_update(target_version: str | None = None) -> dict[str, Any]:
    # Allow retry even if a previous awaiting_restart was stuck.
    st = resolve_stale_update_status()
    if st.get("state") == "running" and _thread_alive():
        return {"ok": False, "error": "یک عملیات آپدیت در حال اجراست", "status": st}
    if st.get("awaiting_restart"):
        # Still genuinely waiting — don't start a second update.
        age = _age_seconds(st.get("finished_at")) or 0
        if age < AWAITING_RESTART_TIMEOUT_SEC:
            return {
                "ok": False,
                "error": "سرویس در حال راه‌اندازی مجدد است؛ چند دقیقه صبر کنید یا صفحه را تازه کنید.",
                "status": st,
            }
    return _start_update_thread(target_version)


def _degraded_update_page_context(*, error: str | None = None) -> dict[str, Any]:
    """Minimal update-tab context so /settings?tab=update never hard-500s."""
    from app.services.update_channel import badge_context, channel_context

    ch_ctx = channel_context()
    badge = badge_context()
    local = local_version()
    label = "بررسی آپدیت ناموفق — صفحه را تازه کنید یا بعداً دوباره تلاش کنید."
    info = {
        "version": local,
        "remote_version": None,
        "update_available": False,
        "label": label,
        "tone": "warn",
        "checked": False,
        "channel": ch_ctx["channel"],
        "channel_label": ch_ctx["channel_label"],
    }
    status = resolve_stale_update_status()
    return {
        "update_info": info,
        "update": info,
        "status": status,
        "local_version": local,
        "steps": [{"key": k, "label": lab, "percent": pct} for k, lab, pct in STEPS],
        "show_ops": bool(
            status.get("awaiting_restart")
            or status.get("restart_required")
            or status.get("state") in {"running", "error"}
        ),
        "changelog": {"title": "چنج‌لاگ", "blocks": [], "has_notes": False},
        "update_channel": ch_ctx["channel"],
        "update_channel_label": ch_ctx["channel_label"],
        "update_channels": ch_ctx["channels"],
        "deployed_channel": badge["deployed_channel"],
        "deployed_channel_label": badge["deployed_channel_label"],
        "migration_preflight": {
            "checked": False,
            "blocked": False,
            "tone": "warn",
            "label": "بررسی مایگریشن ناموفق",
            "message": (error or "").strip()
            or "بررسی مایگریشن کانال انجام نشد. بعداً دوباره تلاش کنید.",
            "channel": ch_ctx["channel"],
        },
        "can_start_update": False,
        "channel_check_message": label,
        "channel_check_tone": "warn",
    }


async def update_page_context(*, force_check: bool = False) -> dict[str, Any]:
    """Build update-tab context; never raises — soft-fails to a usable page."""
    try:
        return await _update_page_context_impl(force_check=force_check)
    except Exception as e:
        logger.exception("update_page_context failed: %s", e)
        return _degraded_update_page_context(error="بررسی آپدیت با خطا روبه‌رو شد.")


async def _update_page_context_impl(*, force_check: bool = False) -> dict[str, Any]:
    from app.services.release_notes import (
        changelog_for_update_page,
        fetch_remote_release_notes,
    )
    from app.services.update_channel import (
        badge_context,
        channel_context,
        get_update_channel,
        migration_preflight,
    )
    channel = get_update_channel()
    info = await check_github_update(force=force_check, channel=channel)
    status = resolve_stale_update_status()
    awaiting = bool(status.get("awaiting_restart"))
    show_ops = bool(
        awaiting
        or status.get("restart_required")
        or status.get("state") in {"running", "error"}
    )
    local = local_version()
    remote = info.get("remote_version") if isinstance(info, dict) else None
    remote_notes = None
    if remote and is_newer(str(remote), local):
        remote_notes = await fetch_remote_release_notes(
            force=force_check, channel=channel
        )
    preflight = await migration_preflight(channel, force=force_check)
    ch_ctx = channel_context(channel)
    badge = badge_context()
    can_start = bool(info.get("update_available")) and not bool(preflight.get("blocked"))
    changelog = changelog_for_update_page(
        local=local, remote=remote, remote_notes=remote_notes
    )
    ch_label = ch_ctx["channel_label"]
    # Keep update-status copy separate from migration_preflight — the update
    # page renders those in two flashes; duplicating the migration text made
    # the error appear twice after a channel switch.
    if info.get("update_available") and remote:
        check_message = (
            f"برای کانال {ch_label} آپدیت جدید هست: {local} → {remote}"
        )
        check_tone = "warn"
    elif info.get("checked"):
        check_message = (
            f"برای کانال {ch_label} آپدیت جدیدی نیست — روی آخرین نسخه هستید."
        )
        check_tone = "ok"
    else:
        check_message = str(info.get("label") or "بررسی آپدیت ناموفق")
        check_tone = "warn"
    if preflight.get("blocked"):
        check_tone = "err"
    return {
        "update_info": info,
        "update": info,  # sidebar badge on settings tab
        "status": status,
        "local_version": local,
        "steps": [{"key": k, "label": lab, "percent": pct} for k, lab, pct in STEPS],
        "show_ops": show_ops,
        "changelog": changelog,
        "update_channel": ch_ctx["channel"],
        "update_channel_label": ch_label,
        "update_channels": ch_ctx["channels"],
        "deployed_channel": badge["deployed_channel"],
        "deployed_channel_label": badge["deployed_channel_label"],
        "migration_preflight": preflight,
        "can_start_update": can_start,
        "channel_check_message": check_message,
        "channel_check_tone": check_tone,
    }
