"""Update from a GitHub source archive when git is not available.

The archive is downloaded and extracted to a staging folder first. Files it
replaces are copied aside before the overlay, so a failed check after the overlay
can put the previous files back and remove the ones the archive added.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

ARCHIVE_MAX_BYTES = 200 * 1024 * 1024
ARCHIVE_MAX_UNCOMPRESSED = 512 * 1024 * 1024
ARCHIVE_MIN_BYTES = 1000
DOWNLOAD_TIMEOUT_SEC = 120
# Runtime state that an archive must never overwrite.
PRESERVED_TOP = frozenset({".env", "data", ".venv", ".git"})


def archive_url(repo: str, ref: str, *, is_version: bool) -> tuple[str, str]:
    """Download URL and label for a branch name or a release tag."""
    ref_name = (ref or "main").strip() or "main"
    if ref_name.startswith(("v", "V")) or is_version:
        tag = ref_name if ref_name.startswith(("v", "V")) else f"v{ref_name.lstrip('vV')}"
        return f"https://github.com/{repo}/archive/refs/tags/{tag}.zip", tag
    return f"https://github.com/{repo}/archive/refs/heads/{ref_name}.zip", ref_name


def download_archive(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "PGClockBot-Panel-Update"})
    try:
        with urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:  # noqa: S310 - fixed https GitHub URL
            data = resp.read(ARCHIVE_MAX_BYTES + 1)
    except (URLError, HTTPError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"دانلود آرشیو ناموفق: {exc}") from exc
    if not data or len(data) < ARCHIVE_MIN_BYTES:
        raise RuntimeError("آرشیو دانلودشده خالی یا ناقص است")
    if len(data) > ARCHIVE_MAX_BYTES:
        raise RuntimeError("آرشیو آپدیت بیش از حد بزرگ است")
    return data


def extract_archive(data: bytes, staging: Path) -> Path:
    """Extract into ``staging`` after rejecting unsafe or oversized members."""
    zpath = staging / "src.zip"
    zpath.write_bytes(data)
    with zipfile.ZipFile(zpath, "r") as zf:
        total = 0
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError("آرشیو آپدیت مسیر ناامن دارد")
            total += max(0, int(info.file_size or 0))
            if total > ARCHIVE_MAX_UNCOMPRESSED:
                raise RuntimeError("حجم آرشیو آپدیت بیش از حد مجاز است")
        zf.extractall(staging / "tree")
    extracted = [p for p in (staging / "tree").iterdir() if p.is_dir() and p.name != "__MACOSX"]
    if not extracted:
        raise RuntimeError("محتوای آرشیو پیدا نشد")
    return extracted[0]


def _overlay_targets(src: Path) -> list[Path]:
    targets: list[Path] = []
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        top = str(rel.parts[0]) if rel.parts else ""
        if top in PRESERVED_TOP or top.startswith(".env.bak"):
            continue
        targets.append(rel)
    return targets


@dataclass(slots=True)
class OverlaySnapshot:
    """Previous copies of overwritten files and the list of files the overlay added."""

    root: Path
    saved_dir: Path
    replaced: list[Path] = field(default_factory=list)
    added: list[Path] = field(default_factory=list)

    def restore(self) -> None:
        for rel in self.added:
            (self.root / rel).unlink(missing_ok=True)
        for rel in self.replaced:
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.saved_dir / rel, dest)


def overlay(src: Path, root: Path, saved_dir: Path, *, log: Callable[[str], None]) -> OverlaySnapshot:
    """Copy the staged tree over ``root``, saving whatever it replaces first."""
    snapshot = OverlaySnapshot(root=root, saved_dir=saved_dir)
    for rel in _overlay_targets(src):
        dest = root / rel
        if dest.is_file():
            backup = saved_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup)
            snapshot.replaced.append(rel)
        else:
            snapshot.added.append(rel)
    try:
        for rel in [*snapshot.replaced, *snapshot.added]:
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest)
    except OSError as exc:
        # A half-applied overlay is worse than either version: put the old files back.
        snapshot.restore()
        raise RuntimeError(f"اعمال فایل‌های آرشیو ناموفق بود: {exc}") from exc
    log(f"تعداد فایل به‌روز شده: {len(snapshot.replaced) + len(snapshot.added)}")
    return snapshot


def new_staging_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="pgclock-upd-")
