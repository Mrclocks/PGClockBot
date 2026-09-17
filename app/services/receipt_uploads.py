"""Private receipt uploads for Mini App (never under the public media mount)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.config import DATA_DIR

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\u0600-\u06FF-]+")
ALLOWED_RECEIPT_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
MAX_RECEIPT_BYTES = 8 * 1024 * 1024  # 8 MiB
LOCAL_PREFIX = "local:"


def _receipts_dir() -> Path:
    d = DATA_DIR / "private" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
        (DATA_DIR / "private").chmod(0o700)
    except OSError:
        pass
    return d


def sanitize_receipt_name(name: str | None) -> str:
    raw = (name or "receipt.jpg").strip().replace("\\", "/").split("/")[-1]
    cleaned = _SAFE_NAME_RE.sub("_", raw).strip(" ._")
    return (cleaned or "receipt.jpg")[:180]


def is_local_receipt(file_id: str | None) -> bool:
    return bool(file_id) and str(file_id).startswith(LOCAL_PREFIX)


def resolve_local_receipt_path(file_id: str | None) -> Path | None:
    """Resolve ``local:private/receipts/...`` — fail closed on traversal."""
    if not is_local_receipt(file_id):
        return None
    raw = str(file_id)[len(LOCAL_PREFIX) :].replace("\\", "/").lstrip("/")
    if ".." in raw.split("/"):
        return None
    if not raw.startswith("private/receipts/"):
        return None
    full = (DATA_DIR / raw).resolve()
    try:
        full.relative_to(DATA_DIR.resolve())
    except ValueError:
        return None
    return full if full.is_file() else None


async def save_mini_receipt_upload(upload, *, payment_id: int) -> str:
    """Persist UploadFile; return stored receipt id (``local:private/receipts/...``)."""
    if upload is None:
        raise ValueError("فایل رسید لازم است")
    filename = getattr(upload, "filename", None) or ""
    if not str(filename).strip():
        raise ValueError("فایل رسید لازم است")
    display = sanitize_receipt_name(str(filename))
    ext = Path(display).suffix.lower()
    if ext not in ALLOWED_RECEIPT_EXT:
        raise ValueError("فقط تصویر رسید مجاز است")
    content = await upload.read(MAX_RECEIPT_BYTES + 1)
    if not content:
        raise ValueError("فایل خالی است")
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError("حجم رسید حداکثر ۸ مگابایت است")
    stem = f"p{int(payment_id)}_{uuid.uuid4().hex[:20]}{ext}"
    dest = _receipts_dir() / stem
    dest.write_bytes(content)
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return f"{LOCAL_PREFIX}private/receipts/{stem}"
