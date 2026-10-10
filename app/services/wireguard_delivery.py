"""Fetch and parse PasarGuard WireGuard configs for Telegram delivery."""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass

from app.services.pasarguard import extract_sub_token, get_pg

logger = logging.getLogger(__name__)

MAX_WIREGUARD_FILES = 5
MAX_WIREGUARD_BYTES = 64 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\u0600-\u06FF-]+")


@dataclass(frozen=True, slots=True)
class WireGuardFile:
    filename: str
    content: bytes


def _safe_filename(name: str, *, fallback: str) -> str:
    raw = (name or "").strip() or fallback
    cleaned = _SAFE_NAME.sub("_", raw).strip("._") or fallback
    if not cleaned.lower().endswith(".conf"):
        cleaned = f"{cleaned}.conf"
    return cleaned[:80]


def parse_wireguard_payload(data: bytes, *, default_stem: str = "wireguard") -> list[WireGuardFile]:
    """Turn a PG ``/wireguard`` body into one or more ``.conf`` files."""
    if not data:
        return []
    if data[:2] == b"PK":
        return _parse_zip(data)
    text = data.decode("utf-8", errors="replace").strip()
    if "[Interface]" not in text or "PrivateKey" not in text:
        return []
    body = text.encode("utf-8")
    if len(body) > MAX_WIREGUARD_BYTES:
        return []
    return [WireGuardFile(filename=_safe_filename(default_stem, fallback="wireguard.conf"), content=body)]


def _parse_zip(data: bytes) -> list[WireGuardFile]:
    out: list[WireGuardFile] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
            for name in names:
                if len(out) >= MAX_WIREGUARD_FILES:
                    break
                low = name.lower()
                if not low.endswith(".conf"):
                    continue
                try:
                    raw = zf.read(name)
                except Exception:
                    logger.debug("wireguard zip member read failed name=%s", name, exc_info=True)
                    continue
                if not raw or len(raw) > MAX_WIREGUARD_BYTES:
                    continue
                text = raw.decode("utf-8", errors="replace")
                if "[Interface]" not in text:
                    continue
                stem = name.rsplit("/", 1)[-1]
                out.append(
                    WireGuardFile(
                        filename=_safe_filename(stem, fallback=f"wg{len(out) + 1}.conf"),
                        content=raw if isinstance(raw, (bytes, bytearray)) else text.encode("utf-8"),
                    )
                )
    except zipfile.BadZipFile:
        return []
    return out


async def fetch_wireguard_files(
    subscription_url: str | None,
    *,
    username: str | None = None,
) -> list[WireGuardFile]:
    """Best-effort fetch; empty list when the service has no WireGuard config."""
    token = extract_sub_token(subscription_url)
    if not token:
        return []
    try:
        client = get_pg()
        payload = await client.subscription_wireguard_bytes(
            token, subscription_url=subscription_url
        )
    except Exception:
        logger.warning("wireguard fetch failed", exc_info=True)
        return []
    if not payload:
        return []
    stem = (username or "wireguard").strip() or "wireguard"
    try:
        return parse_wireguard_payload(payload, default_stem=stem)[:MAX_WIREGUARD_FILES]
    except Exception:
        logger.warning("wireguard parse failed", exc_info=True)
        return []
