"""Fetch and parse PasarGuard WireGuard configs for Telegram delivery."""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from app.services.pasarguard import extract_sub_token, get_pg

logger = logging.getLogger(__name__)

# Telegram sends at most ten documents in one album.
MAX_WIREGUARD_FILES = 10
MAX_WIREGUARD_BYTES = 64 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._؀-ۿ-]+")

# Why a fetch produced no files; the delivery decides what to tell the customer and the admin.
REASON_OK = "ok"
REASON_NONE = "none"  # the panel answered but has no WireGuard config for this service
REASON_DISABLED = "disabled"  # the WireGuard subscription format is switched off in the panel
REASON_UNAVAILABLE = "unavailable"  # the panel could not be reached or answered with an error
FORMAT_DISABLED_STATUS = 406


@dataclass(frozen=True, slots=True)
class WireGuardFile:
    filename: str
    content: bytes


@dataclass(frozen=True, slots=True)
class WireGuardFetch:
    files: list[WireGuardFile]
    total: int  # configs the panel offered, before the album limit
    reason: str


def has_wireguard(info: dict[str, Any] | None) -> bool:
    """Whether the panel assigned this service a WireGuard peer address.

    The panel allocates peer addresses only for users that can reach a WireGuard inbound, so an
    address means the subscription should carry ``.conf`` files.
    """
    if not isinstance(info, dict):
        return False
    proxy_settings = info.get("proxy_settings")
    wireguard = proxy_settings.get("wireguard") if isinstance(proxy_settings, dict) else None
    peer_ips = wireguard.get("peer_ips") if isinstance(wireguard, dict) else None
    return isinstance(peer_ips, list) and any(str(ip).strip() for ip in peer_ips)


def _safe_filename(name: str, *, fallback: str) -> str:
    raw = (name or "").strip() or fallback
    cleaned = _SAFE_NAME.sub("_", raw).strip("._") or fallback
    if not cleaned.lower().endswith(".conf"):
        cleaned = f"{cleaned}.conf"
    return cleaned[:80]


def parse_wireguard_archive(data: bytes, *, default_stem: str = "wireguard") -> tuple[list[WireGuardFile], int]:
    """Turn a PG ``/wireguard`` body into ``.conf`` files plus the number the panel offered."""
    if not data:
        return [], 0
    if data[:2] == b"PK":
        return _parse_zip(data)
    text = data.decode("utf-8", errors="replace").strip()
    if "[Interface]" not in text or "PrivateKey" not in text:
        return [], 0
    body = text.encode("utf-8")
    if len(body) > MAX_WIREGUARD_BYTES:
        return [], 0
    return [WireGuardFile(filename=_safe_filename(default_stem, fallback="wireguard.conf"), content=body)], 1


def _parse_zip(data: bytes) -> tuple[list[WireGuardFile], int]:
    out: list[WireGuardFile] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in sorted(zf.infolist(), key=lambda item: item.filename):
                if info.is_dir() or not info.filename.lower().endswith(".conf"):
                    continue
                if info.file_size > MAX_WIREGUARD_BYTES:
                    continue
                try:
                    raw = zf.read(info)
                except (zipfile.BadZipFile, RuntimeError, ValueError):
                    logger.debug("wireguard zip member read failed name=%s", info.filename, exc_info=True)
                    continue
                if not raw or "[Interface]" not in raw.decode("utf-8", errors="replace"):
                    continue
                total += 1
                if len(out) < MAX_WIREGUARD_FILES:
                    stem = info.filename.rsplit("/", 1)[-1]
                    out.append(WireGuardFile(filename=_safe_filename(stem, fallback=f"wg{len(out) + 1}.conf"), content=raw))
    except zipfile.BadZipFile:
        return [], 0
    return out, total


def _failure_reason(status: int | None) -> str:
    if status == FORMAT_DISABLED_STATUS:
        return REASON_DISABLED
    if status is None or (status >= 400 and status != 404):
        return REASON_UNAVAILABLE
    return REASON_NONE


async def fetch_wireguard(subscription_url: str | None, *, username: str | None = None) -> WireGuardFetch:
    """Fetch a service's WireGuard files; never raises, the reason says why nothing came back."""
    token = extract_sub_token(subscription_url)
    if not token:
        return WireGuardFetch(files=[], total=0, reason=REASON_NONE)
    try:
        response = await get_pg().subscription_wireguard_bytes(token, subscription_url=subscription_url)
    except Exception:  # delivery must go on; the panel error is logged here
        logger.warning("wireguard fetch failed", exc_info=True)
        return WireGuardFetch(files=[], total=0, reason=REASON_UNAVAILABLE)
    if not response.data:
        return WireGuardFetch(files=[], total=0, reason=_failure_reason(response.status))
    stem = (username or "wireguard").strip() or "wireguard"
    files, total = parse_wireguard_archive(response.data, default_stem=stem)
    return WireGuardFetch(files=files, total=total, reason=REASON_OK if files else REASON_NONE)

