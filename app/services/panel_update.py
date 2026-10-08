from __future__ import annotations

"""In-panel self-update + rollback with persisted progress for the web UI."""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, ROOT_DIR
from app.services.updates import check_github_update, clear_update_cache, is_newer, local_version

logger = logging.getLogger(__name__)

STATUS_FILE = DATA_DIR / "panel_update.json"
SNAPSHOTS_FILE = DATA_DIR / "update_snapshots.json"
_LOCK = threading.RLock()
_THREAD: threading.Thread | None = None
MAX_SNAPSHOTS = 1

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
    ("fetch", "ارتباط با گیت‌هاب", 35),
    ("pull", "اعمال کد", 55),
    ("deps", "نصب وابستگی‌ها", 80),
    ("restart", "راه‌اندازی مجدد سرویس", 95),
    ("done", "تمام شد", 100),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_status() -> dict[str, Any]:
    return {
        "state": "idle",  # idle | running | done | error
        "mode": "update",  # update | rollback
        "percent": 0,
        "step": "",
        "step_key": "",
        "message": "آماده برای آپدیت یا بازگشت",
        "log": [],
        "started_at": None,
        "finished_at": None,
        "from_version": local_version(),
        "to_version": None,
        "error": None,
        "snapshot_id": None,
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
        log.append(f"{datetime.now().strftime('%H:%M:%S')} {line}")
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


def _python_bin() -> str:
    root = _repo_root()
    venv_py = root / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def _pip_install() -> tuple[int, str]:
    py = _python_bin()
    root = _repo_root()
    req = root / "requirements.txt"
    if not req.exists():
        return 0, "no requirements.txt"
    # Skip pip when requirements.txt hash is unchanged (big win on weak VPS)
    hash_file = root / ".venv" / ".requirements.sha256"
    try:
        import hashlib

        digest = hashlib.sha256(req.read_bytes()).hexdigest()
        if hash_file.exists() and hash_file.read_text(encoding="utf-8").strip() == digest:
            return 0, "requirements unchanged — skipped pip"
    except Exception:
        digest = None
    code, out = _run(
        [
            py,
            "-m",
            "pip",
            "install",
            "-q",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(req),
        ],
        timeout=600,
    )
    if code == 0 and digest:
        try:
            hash_file.parent.mkdir(parents=True, exist_ok=True)
            hash_file.write_text(digest + "\n", encoding="utf-8")
        except Exception:
            pass
    return code, out



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
                    pass
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


def _update_via_archive(root: Path, *, ref: str | None = None) -> None:
    """Download a branch/tag zip from GitHub and overlay onto install dir.

    ``ref`` is a branch name (default ``main``) or a release tag like ``v3.0.3``.
    """
    import tempfile
    import zipfile
    from urllib.error import URLError, HTTPError
    from urllib.request import Request, urlopen

    from app.version import GITHUB_REPO

    ref_name = (ref or "main").strip() or "main"
    if ref_name.startswith(("v", "V")) or _looks_like_version(ref_name):
        tag = ref_name if ref_name.startswith(("v", "V")) else f"v{ref_name.lstrip('vV')}"
        url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"
        label = tag
    else:
        url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{ref_name}.zip"
        label = ref_name
    _append_log(f"دانلود آرشیو گیت‌هاب ({label})…")
    req = Request(url, headers={"User-Agent": "PGClockBot-Panel-Update"})
    try:
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
    except (URLError, HTTPError, TimeoutError, OSError) as e:
        raise RuntimeError(f"دانلود آرشیو ناموفق: {e}") from e
    if not data or len(data) < 1000:
        raise RuntimeError("آرشیو دانلودشده خالی یا ناقص است")
    if len(data) > 200 * 1024 * 1024:
        raise RuntimeError("آرشیو آپدیت بیش از حد بزرگ است")

    preserve_top = {".env", "data", ".venv", ".git"}
    with tempfile.TemporaryDirectory(prefix="pgclock-upd-") as tmp:
        zpath = Path(tmp) / "src.zip"
        zpath.write_bytes(data)
        with zipfile.ZipFile(zpath, "r") as zf:
            # Reject zip-slip / oversized archives before extraction
            max_uncompressed = 512 * 1024 * 1024
            total = 0
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise RuntimeError("آرشیو آپدیت مسیر ناامن دارد")
                total += max(0, int(info.file_size or 0))
                if total > max_uncompressed:
                    raise RuntimeError("حجم آرشیو آپدیت بیش از حد مجاز است")
            zf.extractall(tmp)
        extracted = [p for p in Path(tmp).iterdir() if p.is_dir() and p.name != "__MACOSX"]
        if not extracted:
            raise RuntimeError("محتوای آرشیو پیدا نشد")
        src = extracted[0]
        copied = 0
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(src)
            if rel.parts and rel.parts[0] in preserve_top:
                continue
            if rel.parts and str(rel.parts[0]).startswith(".env.bak"):
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied += 1
        _append_log(f"تعداد فایل به‌روز شده: {copied}")


def _looks_like_version(value: str | None) -> bool:
    from app.services.updates import normalize_version_tag

    return bool(normalize_version_tag(value))


def _git_head() -> tuple[str, str]:
    git = _git_bin()
    if not git:
        return "", "main"
    root = _repo_root()
    code, sha = _run([git, "rev-parse", "HEAD"], cwd=root)
    sha = (sha or "").strip()
    _, branch = _run([git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    branch = (branch or "main").strip() or "main"
    if branch == "HEAD":
        branch = "detached"
    if code != 0 or not sha:
        return "", branch
    return sha, branch


def list_snapshots() -> list[dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SNAPSHOTS_FILE.exists():
        return []
    try:
        raw = json.loads(SNAPSHOTS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            items = [x for x in raw if isinstance(x, dict) and x.get("sha")]
            # Keep only the newest rollback point
            if len(items) > MAX_SNAPSHOTS:
                items = items[:MAX_SNAPSHOTS]
                _save_snapshots(items)
            return items
    except Exception:
        pass
    return []


def _save_snapshots(items: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:MAX_SNAPSHOTS], ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SNAPSHOTS_FILE)


def create_snapshot(*, reason: str = "before_update") -> dict[str, Any] | None:
    """Save current HEAD so the admin can roll back later (only one point kept)."""
    root = _repo_root()
    if not (root / ".git").exists():
        return None
    try:
        sha, branch = _git_head()
    except Exception as e:
        logger.warning("snapshot skipped: %s", e)
        return None
    if not sha:
        return None
    items = list_snapshots()
    # Avoid duplicate consecutive same sha
    if items and items[0].get("sha") == sha:
        return items[0]
    snap = {
        "id": uuid.uuid4().hex[:12],
        "sha": sha,
        "short_sha": sha[:7],
        "version": local_version(),
        "branch": branch,
        "reason": reason,
        "created_at": _now(),
        "label": f"v{local_version()} · {sha[:7]}",
    }
    # Newest only — drop older rollback points
    _save_snapshots([snap])
    return snap


def get_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    for item in list_snapshots():
        if item.get("id") == snapshot_id or item.get("sha") == snapshot_id:
            return item
    return None


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
        logger.exception("panel update/rollback failed: %s", msg)
    else:
        logger.error("panel update/rollback failed: %s", msg)
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


def _do_update(target_version: str | None) -> None:
    try:
        root = _repo_root()
        write_status(
            {
                "state": "running",
                "mode": "update",
                "percent": 0,
                "error": None,
                "finished_at": None,
                "started_at": _now(),
                "from_version": local_version(),
                "to_version": target_version,
                "log": [],
                "snapshot_id": None,
                "awaiting_restart": False,
                "restart_required": False,
                "pre_boot_id": None,
                "step": "",
                "step_key": "",
                "message": "شروع آپدیت…",
            }
        )
        _set_step("prepare", "شروع آپدیت…")
        _append_log(f"root={root}")

        git = _ensure_git()
        if git:
            _append_log(f"git={git}")
        else:
            _append_log("git در دسترس نیست — از آرشیو zip استفاده می‌شود")

        _set_step("backup", "ثبت نقطه بازگشت + پشتیبان .env")
        snap = create_snapshot(reason="before_update") if git else None
        if snap:
            write_status({"snapshot_id": snap["id"]})
            _append_log(f"نقطه بازگشت ذخیره شد: {snap['label']}")
        else:
            _append_log("هشدار: نقطه بازگشت git ساخته نشد")

        env_path = root / ".env"
        if env_path.exists():
            bak = root / f".env.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(env_path, bak)
            _append_log(f"پشتیبان env: {bak.name}")

        from app.services.update_channel import channel_branch, channel_label_fa, get_update_channel

        channel = get_update_channel()
        branch = channel_branch(channel)
        _append_log(f"کانال آپدیت: {channel_label_fa(channel)} ({branch})")

        use_git = bool(git and (root / ".git").exists())
        if use_git:
            _set_step("fetch", f"git fetch origin {branch}…")
            code, out = _run(
                [git, "fetch", "--no-tags", "--prune", "origin", branch],
                cwd=root,
                timeout=120,
            )
            if out:
                _append_log(out.splitlines()[-1][:200])
            if code != 0:
                _append_log(f"git fetch ناموفق — سوییچ به zip: {(out or '')[:160]}")
                use_git = False
            else:
                remote_ref = f"origin/{branch}"
                _set_step("pull", f"دریافت کد ({remote_ref})…")
                # Deploy the configured update channel (main=stable / dev).
                code, out = _run(
                    [git, "checkout", "-f", "-B", branch, remote_ref],
                    cwd=root,
                    timeout=120,
                )
                if code != 0:
                    _append_log(
                        f"checkout {branch} ناموفق — سوییچ به zip: {(out or '')[:160]}"
                    )
                    use_git = False
                else:
                    code2, out2 = _run(
                        [git, "reset", "--hard", remote_ref], cwd=root, timeout=120
                    )
                    if code2 != 0:
                        _append_log(f"reset ناموفق — سوییچ به zip: {(out2 or '')[:160]}")
                        use_git = False
                    else:
                        _run(
                            [
                                git,
                                "clean",
                                "-fd",
                                "--exclude=.env",
                                "--exclude=data",
                                "--exclude=.venv",
                                "--exclude=.env.bak.*",
                            ],
                            cwd=root,
                            timeout=60,
                        )
                        _append_log(f"کد با {remote_ref} همگام شد")
                        if out2:
                            _append_log(out2.splitlines()[-1][:200])

        if not use_git:
            _set_step("fetch", "دانلود نسخه جدید…")
            _update_via_archive(root, ref=branch)
            _set_step("pull", "اعمال فایل‌ها…")
            _append_log("کد از آرشیو گیت‌هاب اعمال شد")

        from app.services.update_channel import set_deployed_channel

        # Code tip is now this channel — badge follows DEPLOYED_CHANNEL after restart.
        set_deployed_channel(channel)
        _append_log(f"کانال نصب‌شده: {channel_label_fa(channel)}")

        _set_step("deps", "نصب پکیج‌های پایتون…")
        code, out = _pip_install()
        if code != 0:
            raise RuntimeError(f"pip install ناموفق: {out[:400]}")
        _append_log("وابستگی‌ها نصب شد")

        ver_file_ver = _read_local_version_file(root)
        new_ver = ver_file_ver or target_version
        write_status({"to_version": new_ver or target_version})

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
    except Exception as e:
        _finish_error(e)


def _do_rollback(snapshot_id: str) -> None:
    try:
        snap = get_snapshot(snapshot_id)
        if not snap:
            raise RuntimeError("نقطه بازگشت پیدا نشد")
        sha = snap["sha"]
        root = _repo_root()
        git = _ensure_git()
        if not git:
            raise RuntimeError(
                "برای بازگشت به نسخه قبلی به git نیاز است. نصب کنید: apt install -y git"
            )
        write_status(
            {
                "state": "running",
                "mode": "rollback",
                "percent": 0,
                "error": None,
                "finished_at": None,
                "started_at": _now(),
                "from_version": local_version(),
                "to_version": snap.get("version"),
                "log": [],
                "snapshot_id": snap.get("id"),
                "awaiting_restart": False,
                "restart_required": False,
                "pre_boot_id": None,
                "step": "",
                "step_key": "",
                "message": "شروع بازگشت…",
            }
        )
        _set_step("prepare", f"شروع بازگشت به {snap.get('label')}")
        _append_log(f"root={root}")
        _append_log(f"git={git}")

        if not (root / ".git").exists():
            raise RuntimeError(f"مخزن git پیدا نشد: {root}")

        # Snapshot current state before rolling back (so they can undo the undo)
        _set_step("backup", "ثبت وضعیت فعلی قبل از بازگشت")
        pre = create_snapshot(reason="before_rollback")
        if pre:
            _append_log(f"وضعیت فعلی هم ذخیره شد: {pre['label']}")

        env_path = root / ".env"
        if env_path.exists():
            bak = root / f".env.bak.rollback.{datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(env_path, bak)
            _append_log(f"پشتیبان env: {bak.name}")

        _set_step("fetch", "بررسی دسترسی به کامیت…")
        code, out = _run([git, "cat-file", "-t", sha], cwd=root, timeout=30)
        if code != 0 or "commit" not in (out or ""):
            _run([git, "fetch", "--all", "--tags"], cwd=root, timeout=180)
            code, out = _run([git, "cat-file", "-t", sha], cwd=root, timeout=30)
            if code != 0:
                raise RuntimeError(f"کامیت {sha[:7]} در دسترس نیست")

        _set_step("pull", f"بازگردانی کد به {sha[:7]}…")
        code, out = _run([git, "reset", "--hard", sha], cwd=root, timeout=120)
        if code != 0:
            raise RuntimeError(f"reset ناموفق: {out[:300]}")
        _run(
            [
                git,
                "clean",
                "-fd",
                "--exclude=.env",
                "--exclude=data",
                "--exclude=.venv",
                "--exclude=.env.bak.*",
            ],
            cwd=root,
            timeout=60,
        )
        _append_log(f"کد به {sha[:7]} برگشت")

        _set_step("deps", "نصب دوباره وابستگی‌ها…")
        code, out = _pip_install()
        if code != 0:
            raise RuntimeError(f"pip install ناموفق: {out[:400]}")

        new_ver = _read_local_version_file(root) or snap.get("version")
        write_status({"to_version": new_ver})

        _set_step("restart", "راه‌اندازی مجدد…")
        ok, note = _restart_service()
        _append_log(note)
        if not ok:
            _finish_ok(
                f"بازگشت به {new_ver or sha[:7]} انجام شد؛ ریستارت خودکار ممکن نشد — "
                "sudo systemctl restart pgclockbot",
                restart_required=True,
            )
            return
        time.sleep(0.4)
        _finish_ok(
            f"بازگشت به نسخه {new_ver or sha[:7]} انجام شد — سرویس در حال راه‌اندازی مجدد است. "
            "ممکن است چند دقیقه طول بکشد؛ از صفحه خارج نشوید و رفرش نکنید. صفحه به‌صورت خودکار تازه می‌شود.",
            awaiting_restart=True,
        )
    except Exception as e:
        _finish_error(e)


def _git_checkout_tag(root: Path, git: str, tag: str) -> bool:
    """Fetch tags and hard-reset onto ``tag``. Returns True on success."""
    code, out = _run([git, "fetch", "--tags", "--force", "--prune", "origin"], cwd=root, timeout=180)
    if out:
        _append_log(out.splitlines()[-1][:200])
    if code != 0:
        _append_log(f"git fetch --tags ناموفق: {(out or '')[:160]}")
        return False
    # Prefer explicit refs/tags/<tag>, then bare tag name.
    for ref in (f"refs/tags/{tag}", tag):
        code, out = _run([git, "rev-parse", "--verify", ref], cwd=root, timeout=30)
        if code != 0:
            continue
        code2, out2 = _run(
            [git, "checkout", "-f", "-B", f"rollback-{tag.lstrip('vV')}", ref],
            cwd=root,
            timeout=120,
        )
        if code2 != 0:
            _append_log(f"checkout {ref} ناموفق: {(out2 or '')[:160]}")
            continue
        _run(
            [
                git,
                "clean",
                "-fd",
                "--exclude=.env",
                "--exclude=data",
                "--exclude=.venv",
                "--exclude=.env.bak.*",
            ],
            cwd=root,
            timeout=60,
        )
        _append_log(f"کد روی تگ {tag} قرار گرفت")
        return True
    return False


def _do_rollback_to_version(version: str) -> None:
    """Roll the install back to a published GitHub release/tag."""
    from app.services.updates import normalize_version_tag

    try:
        ver = normalize_version_tag(version)
        if not ver:
            raise RuntimeError("نسخه بازگشت نامعتبر است")
        tag = f"v{ver}"
        root = _repo_root()
        write_status(
            {
                "state": "running",
                "mode": "rollback",
                "percent": 0,
                "error": None,
                "finished_at": None,
                "started_at": _now(),
                "from_version": local_version(),
                "to_version": ver,
                "log": [],
                "snapshot_id": None,
                "awaiting_restart": False,
                "restart_required": False,
                "pre_boot_id": None,
                "step": "",
                "step_key": "",
                "message": f"شروع بازگشت به {tag}…",
            }
        )
        _set_step("prepare", f"شروع بازگشت به {tag}")
        _append_log(f"root={root}")

        git = _ensure_git()
        if git:
            _append_log(f"git={git}")
        else:
            _append_log("git در دسترس نیست — از آرشیو zip استفاده می‌شود")

        _set_step("backup", "ثبت نقطه بازگشت + پشتیبان .env")
        snap = create_snapshot(reason="before_rollback") if git else None
        if snap:
            write_status({"snapshot_id": snap["id"]})
            _append_log(f"وضعیت فعلی ذخیره شد: {snap['label']}")

        env_path = root / ".env"
        if env_path.exists():
            bak = root / f".env.bak.rollback.{datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(env_path, bak)
            _append_log(f"پشتیبان env: {bak.name}")

        use_git = bool(git and (root / ".git").exists())
        if use_git:
            _set_step("fetch", f"دریافت تگ {tag}…")
            if not _git_checkout_tag(root, git, tag):
                _append_log("سوییچ به آرشیو zip…")
                use_git = False
            else:
                _set_step("pull", f"اعمال نسخه {tag}…")

        if not use_git:
            _set_step("fetch", f"دانلود آرشیو {tag}…")
            _update_via_archive(root, ref=tag)
            _set_step("pull", "اعمال فایل‌ها…")
            _append_log(f"کد از آرشیو {tag} اعمال شد")

        from app.services.update_channel import set_deployed_channel

        # Published release tags track the stable channel.
        set_deployed_channel("main")
        _append_log("کانال نصب‌شده: پایدار (main)")

        _set_step("deps", "نصب دوباره وابستگی‌ها…")
        code, out = _pip_install()
        if code != 0:
            raise RuntimeError(f"pip install ناموفق: {out[:400]}")

        new_ver = _read_local_version_file(root) or ver
        write_status({"to_version": new_ver})

        _set_step("restart", "راه‌اندازی مجدد…")
        ok, note = _restart_service()
        _append_log(note)
        if not ok:
            _finish_ok(
                f"بازگشت به {new_ver} انجام شد؛ ریستارت خودکار ممکن نشد — "
                "sudo systemctl restart pgclockbot",
                restart_required=True,
            )
            return
        time.sleep(0.4)
        _finish_ok(
            f"بازگشت به نسخه {new_ver} انجام شد — سرویس در حال راه‌اندازی مجدد است. "
            "ممکن است چند دقیقه طول بکشد؛ از صفحه خارج نشوید و رفرش نکنید. صفحه به‌صورت خودکار تازه می‌شود.",
            awaiting_restart=True,
        )
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


def _start_thread(target, *args) -> dict[str, Any]:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return {"ok": False, "error": "یک عملیات آپدیت/بازگشت در حال اجراست"}
        # Mark running immediately so /update/status never returns stale awaiting.
        mode = "update" if target is _do_update else "rollback"
        seed = _default_status()
        seed.update(
            {
                "state": "running",
                "mode": mode,
                "started_at": _now(),
                "message": "شروع عملیات…",
                "awaiting_restart": False,
                "restart_required": False,
                "pre_boot_id": None,
                "log": [],
            }
        )
        if mode == "update" and args:
            seed["to_version"] = args[0]
        if mode == "rollback" and args:
            # snapshot id or target version string
            arg0 = str(args[0] or "").strip()
            if target is _do_rollback_to_version:
                seed["to_version"] = arg0.lstrip("vV")
            else:
                seed["snapshot_id"] = arg0
        _replace_status(seed)
        _THREAD = threading.Thread(target=target, args=args, name="panel-update", daemon=True)
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
    from app.services.updates import is_newer, is_same_or_newer

    st = read_status()
    local = local_version()
    to_ver = str(st.get("to_version") or "").strip()
    mode = str(st.get("mode") or "update")

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
        version_ok = False
        if to_ver:
            if mode == "rollback":
                # Rollback target should match (not merely local >= target).
                version_ok = not is_newer(to_ver, local) and not is_newer(local, to_ver)
            else:
                version_ok = is_same_or_newer(local, to_ver)
        else:
            version_ok = restarted

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
    return _start_thread(_do_update, target_version)


def start_rollback(snapshot_id: str) -> dict[str, Any]:
    if not get_snapshot(snapshot_id):
        return {"ok": False, "error": "نقطه بازگشت معتبر نیست"}
    st = resolve_stale_update_status()
    if st.get("state") == "running" and _thread_alive():
        return {"ok": False, "error": "یک عملیات در حال اجراست", "status": st}
    return _start_thread(_do_rollback, snapshot_id)


def start_rollback_to_version(version: str, *, allowed: list[str] | None = None) -> dict[str, Any]:
    """Roll back to a published version (must be in ``allowed`` when provided)."""
    from app.services.updates import _parse_ver, normalize_version_tag

    ver = normalize_version_tag(version)
    if not ver:
        return {"ok": False, "error": "نسخه بازگشت نامعتبر است"}
    if allowed is not None:
        allowed_norm = {normalize_version_tag(x) for x in allowed if normalize_version_tag(x)}
        if ver not in allowed_norm:
            return {"ok": False, "error": "فقط از میان ۳ نسخهٔ اخیر گیت‌هاب می‌توانید برگردید"}
    if _parse_ver(local_version()) == _parse_ver(ver):
        return {"ok": False, "error": "همین الان روی این نسخه هستید"}
    st = resolve_stale_update_status()
    if st.get("state") == "running" and _thread_alive():
        return {"ok": False, "error": "یک عملیات در حال اجراست", "status": st}
    if st.get("awaiting_restart"):
        age = _age_seconds(st.get("finished_at")) or 0
        if age < AWAITING_RESTART_TIMEOUT_SEC:
            return {
                "ok": False,
                "error": "سرویس در حال راه‌اندازی مجدد است؛ چند دقیقه صبر کنید.",
                "status": st,
            }
    return _start_thread(_do_rollback_to_version, ver)


async def update_page_context(*, force_check: bool = False) -> dict[str, Any]:
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
    from app.services.updates import fetch_recent_versions, is_newer

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
    rollback_versions = await fetch_recent_versions(limit=3, force=force_check)
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
        "snapshots": list_snapshots(),
        "rollback_versions": rollback_versions,
        "can_rollback": bool(rollback_versions),
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
